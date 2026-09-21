"""Retrieval pipeline: FTS5 + vector KNN + RRF fusion + re-ranking.

Implements the recall half of the memory loop (spec 8.2 / 8.3):

    retrieval  = FTS5 (lexical, jieba-segmented query) ⊕ vec0 (semantic KNN)
    fusion     = Reciprocal Rank Fusion (RRF)
    relevance  = min(1, rrf / rrf_ceiling)      ← absolute, not per-query
    re-rank    = 0.4·relevance + 0.2·effective_importance + 0.2·recency_norm
                 + 0.2·trust_score
    trust      = 0.6·confidence + 0.4·source_credibility

**Every term is on an absolute 0..1 scale.** That is the point of the design and
it applies to the relevance term too: a rank-based score rescaled over the
candidate set (min-max) makes the strongest candidate *always* exactly 1.0 and
the weakest exactly 0.0, so a lone mediocre match is indistinguishable from a
perfect one and the meaning of the score depends on who else happened to be
retrieved. ``rrf_ceiling`` is the score a perfect match would earn (rank 0 in
both lists), so dividing by it yields a value that means the same thing in every
query.

The importance term is the fact's **effective** importance: the value written at
extraction time plus the saturating reuse bonus from
:mod:`~atom_memory.reinforce`, with the stored reinforcement snapshot decayed to
the current instant (``reinforce.adjust``). Reuse therefore strengthens ranking,
but bounded and with a diminishing marginal effect — and it fades again if the
fact stops being used.

Recency is likewise a half-life decay rather than a min-max rescale of the
candidate ages: ages are shifted so the newest candidate is the reference
(:func:`~atom_memory.db.age_offset`) and then decayed
(:func:`~atom_memory.db.recency_credit`). It is measured from ``last_used_at``
where the fact has been used, so a long-lived fact that is still in active use is
not aged out for being old. See :data:`RECENCY_HALF_LIFE_DAYS` for the tuning.

Two filters decide whether a candidate is *relevant enough to answer with*,
because a memory that always returns its least-bad row cannot say "I don't know":

- ``max_vector_distance`` gates the semantic half on the raw cosine distance.
  Rank alone cannot express "all of these are far away" — the nearest of twenty
  bad matches still ranks first — so the distance is the only honest signal for
  a floor.
- ``min_relevance`` gates the fused score. Only meaningful together with the
  distance gate; on its own every candidate that made either top-k clears it,
  which is exactly the trap a rank-based floor sets.

All lookups are hard-scoped to ``user_id`` and only ``active`` facts are
considered.
"""

from __future__ import annotations

import asyncio
import logging
import sqlite3
from typing import Callable, Dict, List, Mapping, Optional, Sequence, Tuple

from .config import MemConfig
from .db import MS_PER_DAY, age_offset, now_ms, recency_credit
from .reinforce import adjust, effective_importance
from .scope import ScopeStore, ScopeView, resolution_for

logger = logging.getLogger(__name__)

# Source-of-truth credibility scores (spec 8.3).
SOURCE_CREDIBILITY: Dict[str, float] = {
    "user_explicit": 1.00,
    "user_confirmed": 0.95,
    "system_inferred_high": 0.80,
    "external_tool": 0.70,
    "indirect_inferred": 0.60,
    "system_inferred_low": 0.50,
    "model_generated": 0.30,
}

# Default weights for the re-ranking formula (spec 8.3). Overridable through
# :class:`~atom_memory.config.MemConfig`, because "how much should relevance
# outweigh importance" is a product decision, not a constant of nature.
W_RRF = 0.4
W_IMPORTANCE = 0.2
W_RECENCY = 0.2
W_TRUST = 0.2

T_TRUST_CONFIDENCE = 0.6
T_TRUST_SOURCE = 0.4

# RRF constant. Larger flattens the gap between neighbouring ranks.
RRF_K = 60

# Default relevance floor / distance gate. Both are off by default so the
# library behaves exactly as before unless a deployer opts in; the plugin sets a
# distance gate, because an injected digest that never says "nothing relevant"
# is worse than one that does.
MIN_RELEVANCE = 0.0

# -- recency ------------------------------------------------------------------
#
# Age at which a fact's recency credit halves, and how far back the relative
# shift may reach. See db.age_offset / db.recency_credit for why recency is a
# shifted exponential decay rather than a per-query min-max rescale of ages.
#
# 30 days is deliberately much longer than the summary view's 14: that view
# answers "what is going on right now" for a session-start snapshot, while this
# one answers "which of the things matching *this query* is most current",
# already gated by relevance — so recency here is a tie-breaker among relevant
# facts, not a selector.
RECENCY_HALF_LIFE_DAYS = 30.0

# Cap on the relative shift. It must stay well above the half-life, or the cap
# stops being a backstop and becomes the dominant shaper: at window == half-life
# every candidate more than one half-life older than the newest is flattened onto
# the same credit (0.5), and genuinely different ages stop being distinguished.
# Three half-lives keeps ~3 bits of resolution across the plausible spread.
RECENCY_REFERENCE_WINDOW_DAYS = 3 * RECENCY_HALF_LIFE_DAYS

# A fact's age for recency purposes is measured from the last time it was *used*
# when it has been used, falling back to creation. A long-lived fact that is
# still in active use must not be aged out merely for being old, and reuse is
# what makes it current — the same reason reinforce.py prefers last_used_at.
_AGE_AT_SQL = "COALESCE(last_used_at, created_at)"


def rrf_merge(
    fts: Sequence[str], vec: Sequence[str], k: int = RRF_K
) -> List[tuple]:
    """Fuse the FTS and vector result lists by Reciprocal Rank Fusion.

    Args:
        fts: Ordered list of fact_ids from FTS (best first).
        vec: Ordered list of fact_ids from vector KNN (best first).
        k: RRF constant.

    Returns:
        A list of ``(fact_id, rrf_score)`` sorted descending by score.
    """
    scores: Dict[str, float] = {}
    for rank, fact_id in enumerate(fts):
        scores[fact_id] = scores.get(fact_id, 0.0) + 1.0 / (k + rank + 1)
    for rank, fact_id in enumerate(vec):
        scores[fact_id] = scores.get(fact_id, 0.0) + 1.0 / (k + rank + 1)
    return sorted(scores.items(), key=lambda x: -x[1])


def rrf_ceiling(k: int = RRF_K) -> float:
    """Return the RRF score of a perfect match (rank 0 in both lists).

    Args:
        k: RRF constant.

    Returns:
        The highest score :func:`rrf_merge` can produce for this ``k``.
    """
    return 2.0 / (k + 1)


def relevance_from_rrf(score: float, k: int = RRF_K) -> float:
    """Map a fused RRF score onto the absolute 0..1 relevance scale.

    Args:
        score: A raw :func:`rrf_merge` score.
        k: RRF constant used by that merge.

    Returns:
        ``min(1, score / ceiling)`` — 1.0 only for a top-of-both-lists match,
        ~0.5 for a top-of-one-list match, lower for weaker ranks.
    """
    ceiling = rrf_ceiling(k)
    if ceiling <= 0:
        return 0.0
    return min(1.0, max(0.0, float(score) / ceiling))


class Retriever:
    """Ranked recall of active atomic facts for a user."""

    def __init__(
        self,
        conn: sqlite3.Connection,
        embed_one: Callable[[str], bytes],
        top_k_default: int = 10,
        config: Optional[MemConfig] = None,
    ) -> None:
        """Initialise the retriever.

        Args:
            conn: The SQLite connection.
            embed_one: Callable mapping text to a serialized embedding BLOB.
            top_k_default: Default number of candidates to return.
            config: Configuration carrying the ranking weights and the
                relevance/distance gates. ``None`` uses the library defaults.
        """
        self.conn = conn
        self.embed_one = embed_one
        self.top_k_default = top_k_default
        self.config = config or MemConfig()
        # Indexes that failed during the last search. Surfaced through recall so
        # a caller can tell "nothing matched" from "the index is broken".
        self.last_degraded: List[str] = []
        # Where the last search was resolved to, as a plain dict. Surfaced so a
        # caller can see *why* a result set is narrow (a project scope with no
        # facts looks exactly like an empty store otherwise).
        self.last_scope: Optional[dict] = None

    async def search(
        self,
        user_id: str,
        query: str,
        top_k: Optional[int] = None,
        scope_context: Optional[Mapping] = None,
        conditions: Optional[Mapping] = None,
    ) -> List[dict]:
        """Run the full retrieval pipeline and return ranked facts.

        Args:
            user_id: The user whose memory is searched (hard isolation scope).
            query: The natural-language query.
            top_k: Maximum number of facts to return (defaults to
                ``self.top_k_default``).
            scope_context: The session context payload (see
                :func:`~atom_memory.context.context_from_payload`). When given
                *and* scope awareness is on, the candidate set is the scope's own
                path plus its phases plus condition-matching facts from anywhere,
                and the re-rank adds the scope / condition / phase terms. When
                absent, retrieval is exactly the pre-scope pipeline: one global
                pool, four ranking terms.
            conditions: Conditions of the current context, when the caller knows
                them without a full context payload (``{"doc_type": "proposal"}``
                or ``{key: [values]}``). Folded in with the context's own.

        Returns:
            A list of fact dicts ordered by descending ``final_score``, each
            with keys ``fact_id``, ``subject``, ``predicate``, ``object``,
            ``confidence``, ``importance``, ``source_type``, ``status``,
            ``created_at``, ``relevance`` and ``final_score`` — plus, in
            scope-aware mode, ``scopes`` / ``scope_labels`` / ``conditions`` /
            ``scope_weight`` / ``condition_match`` / ``phase_match``. Candidates
            below ``min_relevance`` are dropped — "nothing relevant" is a valid
            answer.
        """
        k = top_k or self.top_k_default
        query = (query or "").strip()
        self.last_degraded = []
        self.last_scope = None
        if not query:
            return []

        view, scope_sql, scope_args = self._scope_view(
            user_id, scope_context, conditions
        )
        blob = await asyncio.to_thread(self.embed_one, query)

        vec_ids = self._vector_knn(
            user_id,
            blob,
            k,
            max_distance=self.config.max_vector_distance,
            scope_sql=scope_sql,
            scope_args=scope_args,
        )
        fts_ids = self._fts_search(user_id, query, k, scope_sql, scope_args)

        fused = rrf_merge(fts_ids, vec_ids, k=self.config.rrf_k)
        if not fused:
            return []

        # Pull full fact rows for the fused ids.
        facts = self._fetch_facts(user_id, [fid for fid, _ in fused])

        # RRF scores in the same order as fused.
        rrf_scores = dict(fused)
        ranked = self._rerank(facts, rrf_scores, view)
        floor = float(self.config.min_relevance or 0.0)
        if floor > 0.0:
            ranked = [f for f in ranked if f["relevance"] >= floor]
        return ranked[:k]

    # -- scope awareness ------------------------------------------------------

    def _scope_view(
        self,
        user_id: str,
        scope_context: Optional[Mapping],
        conditions: Optional[Mapping],
    ) -> Tuple[Optional[ScopeView], str, list]:
        """Resolve the query's scope and build its candidate filter.

        Args:
            user_id: The owner the query is scoped to. The context payload has no
                say in this: isolation is the query's business, and a payload
                that named a different user must not be able to widen it.
            scope_context: The context payload, or ``None``.
            conditions: Extra conditions supplied by the caller.

        Returns:
            ``(view, sql, args)``. ``view`` is ``None`` — and ``sql`` empty —
            when there is nothing to be scope-aware *about*: scope awareness
            disabled, or a caller that sent no context. That is the switch that
            keeps a scope-blind caller on exactly the pre-scope pipeline.
        """
        if not self.config.scope_aware or scope_context is None:
            return None, "", []
        store = ScopeStore(self.conn, self.config)
        ctx, resolution = resolution_for(
            self.conn,
            self.config,
            scope_context,
            user_id=user_id,
            session_id=str(scope_context.get("session_id") or ""),
            # A query must never create scopes or extend the candidate queue.
            create=False,
        )
        if ctx is None:
            return None, "", []
        condition_map = ctx.condition_map()
        for key, value in (conditions or {}).items():
            values = value if isinstance(value, (list, tuple, set)) else [value]
            for item in values:
                text = str(item).strip().casefold()
                if text:
                    condition_map.setdefault(str(key).strip().casefold(), []).append(text)
        view = store.view(resolution, condition_map)
        self.last_scope = resolution.to_dict(store)
        sql, args = self._scope_predicate(view, condition_map)
        return view, sql, args

    def _scope_predicate(
        self, view: ScopeView, conditions: Mapping[str, Sequence[str]]
    ) -> Tuple[str, list]:
        """Build the SQL predicate that narrows candidates to the query's scope.

        Three disjoint ways in:

        1. the fact is bound to the query scope's path or one of its phases;
        2. the fact is bound somewhere else but its conditions match the current
           context — the design's "条件匹配的其他 scope（降权）", which the re-rank
           then scores at ``W_OTHER``/``W_SIBLING``;
        3. the fact is unbound, i.e. a global fact by the compatibility rule.

        Anything else is *not* a candidate: leaving it in and down-weighting it
        would let an unrelated project's fact win whenever the semantic match was
        strong enough, which is the pollution this whole dimension exists to
        prevent.

        Returns:
            ``(sql, args)`` where ``sql`` is an ``AND (...)`` fragment over the
            alias ``f``.
        """
        visible = view.visible_ids()
        placeholders = ",".join("?" for _ in visible)
        args: list = list(visible)
        sql = (
            "AND (NOT EXISTS (SELECT 1 FROM fact_scope fs0 WHERE fs0.fact_id = f.fact_id) "
            f"OR EXISTS (SELECT 1 FROM fact_scope fs WHERE fs.fact_id = f.fact_id "
            f"AND fs.scope_id IN ({placeholders}))"
        )
        pairs = [
            (key, value)
            for key, values in (conditions or {}).items()
            for value in values
        ]
        if pairs:
            ors = " OR ".join("(fc.key = ? AND fc.value = ?)" for _ in pairs)
            sql += (
                " OR EXISTS (SELECT 1 FROM fact_condition fc "
                f"WHERE fc.fact_id = f.fact_id AND ({ors}))"
            )
            for key, value in pairs:
                args.extend([key, value])
        sql += ") "
        return sql, args

    # -- retrieval primitives -------------------------------------------------

    def _vector_knn(
        self,
        user_id: str,
        blob: bytes,
        k: int,
        max_distance: Optional[float] = None,
        scope_sql: str = "",
        scope_args: Optional[list] = None,
    ) -> List[str]:
        """Return fact_ids from semantic KNN, best first.

        Restricts candidates to the user's active facts via an ``IN``
        subquery; sqlite-vec requires the KNN form with an explicit ``LIMIT``.

        Args:
            user_id: Isolation scope.
            blob: The query embedding.
            k: Maximum number of ids.
            max_distance: Optional cosine-distance ceiling. Rows farther away
                than this are dropped, which is the only way to say "none of
                these are close" — a rank cannot.
            scope_sql: Optional scope/condition predicate over the alias ``f``.
            scope_args: Its bound parameters.

        Returns:
            Fact ids, nearest first.
        """
        scope_args = list(scope_args or [])
        inner = (
            "SELECT f.fact_id FROM facts f WHERE f.user_id = ? "
            "AND f.status = 'active' " + scope_sql
        )
        try:
            if max_distance is None:
                rows = self.conn.execute(
                    f"SELECT fact_id FROM facts_vec WHERE embedding MATCH ? "
                    f"AND fact_id IN ({inner}) ORDER BY distance LIMIT ?",
                    (blob, user_id, *scope_args, k),
                ).fetchall()
            else:
                rows = self.conn.execute(
                    f"SELECT fact_id, distance FROM facts_vec WHERE embedding MATCH ? "
                    f"AND fact_id IN ({inner}) ORDER BY distance LIMIT ?",
                    (blob, user_id, *scope_args, k),
                ).fetchall()
                rows = [r for r in rows if float(r["distance"]) <= max_distance]
            return [r["fact_id"] for r in rows]
        except sqlite3.Error as exc:  # pragma: no cover - defensive
            logger.warning("vector KNN failed: %s", exc)
            self.last_degraded.append("vector")
            return []

    def _fts_search(
        self,
        user_id: str,
        query: str,
        k: int,
        scope_sql: str = "",
        scope_args: Optional[list] = None,
    ) -> List[str]:
        """Return fact_ids matching a jieba-segmented FTS query, best first.

        FTS is best-effort: malformed queries or empty token streams are
        swallowed and return nothing rather than breaking retrieval. The
        failure is recorded on :attr:`last_degraded` so the caller can tell it
        apart from a genuine miss.
        """
        tokens = segment_text(query)
        if not tokens:
            return []
        match = " OR ".join(f'"{t}"' for t in tokens)
        try:
            rows = self.conn.execute(
                "SELECT f.fact_id FROM facts_fts fts "
                "JOIN facts f ON f.fact_id = fts.fact_id "
                "WHERE f.user_id = ? AND f.status = 'active' "
                + scope_sql
                + "AND facts_fts MATCH ? "
                "ORDER BY rank LIMIT ?",
                (user_id, *list(scope_args or []), match, k),
            ).fetchall()
            return [r["fact_id"] for r in rows]
        except sqlite3.Error as exc:
            logger.warning("FTS search failed (%s): %s", match, exc)
            self.last_degraded.append("fts")
            return []

    def _fetch_facts(
        self, user_id: str, fact_ids: Sequence[str]
    ) -> List[dict]:
        """Fetch full fact rows for the given ids (user-scoped)."""
        if not fact_ids:
            return []
        placeholders = ",".join("?" for _ in fact_ids)
        rows = self.conn.execute(
            f"SELECT fact_id, subject, predicate, object, confidence, "
            f"importance, source_type, status, created_at, type, content, "
            f"reinforce_count, last_used_at, {_AGE_AT_SQL} AS age_at "
            f"FROM facts WHERE user_id = ? AND fact_id IN ({placeholders})",
            [user_id, *fact_ids],
        ).fetchall()
        by_id = {r["fact_id"]: dict(r) for r in rows}
        # Preserve the fused order.
        return [by_id[fid] for fid in fact_ids if fid in by_id]

    # -- ranking --------------------------------------------------------------

    def _rerank(
        self,
        facts: List[dict],
        rrf_scores: Dict[str, float],
        view: Optional[ScopeView] = None,
    ) -> List[dict]:
        """Apply the spec 8.3 weighting formula and sort by final_score.

        With a scope view, the design's three extra terms are added
        (``0.20·scope_distance + 0.15·condition_match + 0.05·phase_match``)
        *on top of* the four base terms rather than re-normalising the four: a
        scope-aware query and a scope-blind one then agree on the base terms, and
        a stored weight set keeps meaning the same thing in both modes. Without a
        view the result is byte-identical to the pre-scope library, which is what
        lets an unscoped caller keep its existing ranking.
        """
        if not facts:
            return []

        now = now_ms()
        k = int(self.config.rrf_k)
        # Relevance is absolute: the fused score is divided by what a perfect
        # match would have earned, so a lone mediocre hit no longer scores the
        # same as a top-of-both-lists hit (which min-max over the candidate set
        # would have made it do).
        rel_vals = [
            relevance_from_rrf(rrf_scores.get(f["fact_id"], 0.0), k)
            for f in facts
        ]
        # Reuse feeds ranking through the *effective* importance (base plus the
        # saturating reinforcement bonus); the stored `importance` stays the
        # extractor's original judgement so it can always be reported as-is.
        #
        # The stored reinforcement columns are a *snapshot* taken at
        # last_used_at, so they are decayed to `now` here. Reading them raw would
        # keep a long-unused fact at its year-old strength forever.
        counts = [
            adjust(
                float(f.get("reinforce_count") or 0.0), f.get("last_used_at"), now
            )
            for f in facts
        ]
        eff_vals = [
            effective_importance(float(f.get("importance") or 0.0), count)
            for f, count in zip(facts, counts)
        ]
        age_ms = [
            max(0.0, float(now - int(f.get("age_at") or f["created_at"] or 0)))
            for f in facts
        ]
        # Recency is relative to the newest candidate, with the shift capped so
        # an entirely-old result set still spreads its credits. See
        # db.age_offset for why neither a raw wall-clock age nor an uncapped
        # shift to zero works, and RECENCY_REFERENCE_WINDOW_DAYS for why the cap
        # must stay well above the half-life.
        newest_age = min(age_ms)
        window_ms = RECENCY_REFERENCE_WINDOW_DAYS * MS_PER_DAY
        half_life_ms = RECENCY_HALF_LIFE_DAYS * MS_PER_DAY

        # Absolute, *not* min-max normalised — the same rule as the relevance
        # term above. Min-max rescales the candidate set so the best fact always
        # scores exactly 1.0 and the worst 0.0, which makes the importance
        # term's real magnitude depend on who else happened to be retrieved and
        # lets a negligible relevance gap between two candidates stretch across
        # the full 0.2 weight — enough to cancel the entire reinforcement budget
        # (A_MAX = 0.5 -> at most 0.1 of the final score). The value is already a
        # meaningful 0..1 measure, so the reinforcement ceiling is a real ceiling
        # instead of a per-query rank.
        imp_norm = eff_vals
        recency_norm = [
            recency_credit(
                age_offset(age, newest_age, window_ms), half_life_ms, 0.0
            )
            for age in age_ms
        ]

        w_rrf = float(self.config.w_rrf)
        w_imp = float(self.config.w_importance)
        w_rec = float(self.config.w_recency)
        w_trust = float(self.config.w_trust)
        w_scope = float(self.config.w_scope)
        w_condition = float(self.config.w_condition)
        w_phase = float(self.config.w_phase)

        # Scope is looked up in two batch queries rather than per fact: the
        # bindings and the conditions are the same shape of lookup for every
        # candidate, and doing them one fact at a time is an N+1 on the hot path.
        scopes: Dict[str, Tuple[int, ...]] = {}
        conditions: Dict[str, Tuple[Tuple[str, str], ...]] = {}
        labels: Dict[int, str] = {}
        if view is not None:
            store = view.store
            ids = [f["fact_id"] for f in facts]
            scopes = store.fact_scopes(ids)
            conditions = store.fact_conditions(ids)
            for scope_id in {sid for values in scopes.values() for sid in values}:
                labels[scope_id] = store.label(scope_id)

        ranked: List[dict] = []
        for i, fact in enumerate(facts):
            trust = (
                T_TRUST_CONFIDENCE * float(fact["confidence"])
                + T_TRUST_SOURCE
                * SOURCE_CREDIBILITY.get(fact["source_type"], 0.5)
            )
            final = (
                w_rrf * rel_vals[i]
                + w_imp * imp_norm[i]
                + w_rec * recency_norm[i]
                + w_trust * trust
            )
            item = dict(fact)
            item["relevance"] = round(rel_vals[i], 6)
            item["effective_importance"] = round(eff_vals[i], 6)
            # The decayed reuse count the bonus above was derived from. The raw
            # column is a snapshot from last_used_at and would disagree with it.
            item["strength"] = round(counts[i], 6)
            item["recency"] = round(recency_norm[i], 6)
            if view is not None:
                fact_scopes = scopes.get(fact["fact_id"], ())
                fact_conditions = conditions.get(fact["fact_id"], ())
                scope_w = view.weight_for(fact_scopes)
                condition_w = view.condition_weight(fact_conditions)
                phase_w = view.phase_weight(fact_scopes)
                final += (
                    w_scope * scope_w
                    + w_condition * condition_w
                    + w_phase * phase_w
                )
                item["scopes"] = list(fact_scopes)
                item["scope_labels"] = [labels.get(sid, "global") for sid in fact_scopes]
                item["conditions"] = [
                    {"key": key, "value": value} for key, value in fact_conditions
                ]
                item["scope_weight"] = round(scope_w, 6)
                item["condition_match"] = round(condition_w, 6)
                item["phase_match"] = round(phase_w, 6)
            item["final_score"] = round(final, 4)
            ranked.append(item)

        ranked.sort(key=lambda x: x["final_score"], reverse=True)
        return ranked


# -- helpers ------------------------------------------------------------------


def estimate_tokens(text: str) -> int:
    """Estimate the number of tokens in a text for budget trimming.

    A simple, deterministic heuristic: CJK characters count as one token each,
    and every ``CHARS_PER_TOKEN`` non-CJK characters count as one word token.
    Not an exact tokenizer — just a stable proxy for budgeting.

    Args:
        text: The text to estimate.

    Returns:
        A non-negative integer token estimate.
    """
    if not text or not text.strip():
        return 0
    return token_cost(text)[0]


#: Non-CJK characters per estimated token.
#:
#: English prose and source code run at roughly 4 characters per token, so the
#: previous divisor of 5 under-counted Latin text by 20-25% — enough for a
#: Latin-heavy digest to overshoot a budget it claimed to respect. CJK text is
#: unaffected (one character is one token). Exported because the summary's
#: incremental accounting has to use the same constant as this estimate, or the
#: fast path and the full render stop agreeing.
CHARS_PER_TOKEN = 4


def token_cost(text: str) -> tuple:
    """Return ``(tokens, cjk_chars, other_chars)`` for a text.

    The two counts are what make the estimate *incrementally* computable: the
    token count of an assembled artifact is a pure function of its totals
    (``cjk + max(other // CHARS_PER_TOKEN, 1)``), so a caller that removes a line
    can update the totals instead of re-measuring the whole text. See
    :func:`~atom_memory.summary._select` for the consumer.

    Args:
        text: The text to measure.

    Returns:
        ``(tokens, cjk, other)``.
    """
    if not text or not text.strip():
        return (0, 0, 0)
    cjk = sum(1 for ch in text if "\u4e00" <= ch <= "\u9fff")
    other = len(text) - cjk
    word_tokens = (other // CHARS_PER_TOKEN) if other else 0
    return (cjk + max(word_tokens, 1 if other else 0), cjk, other)


def truncate_to_tokens(text: str, limit: int) -> str:
    """Shorten ``text`` so its estimated token count fits ``limit``.

    One pass, not one measurement per prefix: the estimate is a function of the
    CJK count and the non-CJK count, both of which accumulate as the text is
    walked.

    The ellipsis is budgeted *before* the walk, not appended afterwards. It is
    itself a non-CJK character, so adding it on top of a prefix that already
    spent the whole budget pushed the result one token over the limit — exactly
    when the non-CJK count sat on a ``CHARS_PER_TOKEN`` boundary, which is the
    common case for Latin text. Since this function exists to enforce a
    per-fact ceiling on what is injected, an overrun defeated the cap it was
    called to apply.

    Args:
        text: The text to shorten (returned unchanged when it already fits).
        limit: Maximum estimated tokens.

    Returns:
        The text, cut at the last character that fits, ellipsis included.
    """
    if limit <= 0 or not text:
        return ""
    if token_cost(text)[0] <= limit:
        return text
    # Reserve what the ellipsis will cost so the returned string — ellipsis
    # included — respects `limit`.
    ellipsis_cost = token_cost("…")[0]
    budget = limit - ellipsis_cost
    if budget <= 0:
        # No room for any content alongside the ellipsis. At limit >= 1 the
        # ellipsis alone still fits (and says more than an empty string); below
        # that only the empty string does.
        return "…" if limit >= ellipsis_cost else ""
    cjk = 0
    other = 0
    for index, ch in enumerate(text):
        if "\u4e00" <= ch <= "\u9fff":
            cjk += 1
        else:
            other += 1
        if cjk + max((other // CHARS_PER_TOKEN) if other else 0, 1 if other else 0) > budget:
            return text[:index].rstrip() + "…" if index > 0 else "…"
    return text


def segment_text(text: str) -> List[str]:
    """Segment Chinese text into distinct, FTS-safe tokens.

    Falls back to the raw text split on whitespace if jieba is unavailable.
    Used both for query matching and (via the worker) for FTS indexing.

    Args:
        text: The text to segment.

    Returns:
        A list of distinct non-empty token strings.
    """
    try:
        import jieba

        jieba.setLogLevel(60)  # silence jieba's init banner
        tokens = [t for t in jieba.cut(text) if t and t.strip()]
    except Exception:  # pragma: no cover - jieba is a declared dependency
        tokens = [t for t in text.replace(" ", "").split() if t]
    seen = set()
    out = []
    for t in tokens:
        t = t.strip().replace('"', "").replace("'", "")
        if t and t not in seen:
            seen.add(t)
            out.append(t)
    return out
