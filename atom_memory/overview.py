"""The work overview: what has been worked on, cached and refreshed out of band.

The injected memory snapshot used to be a type-grouped list of atomic facts, and
that view answered the wrong question. A session that opens with
``技术栈: Flask、React、Vite`` and ``内置斜杠命令: 仅 /compact`` learns a handful of
disconnected details and still cannot say what was actually *worked on*, or which
of it is finished. This module builds the material for the replacement: a
**work overview** organised by work unit (project / series / phase / document)
rather than by memory type.

Three pieces, and the split is the whole design:

1. :func:`build_overview_skeleton` — a **deterministic** aggregation over the
   store: per work unit, how many facts it holds, which kinds, its time span, and
   its strongest few titles; plus the topic labels and the store-wide totals. No
   model call, no clock dependency, bounded output.
2. :func:`change_level` — reads the changelog (the ``events`` table) and says how
   much the store actually moved since the cached overview was written.
3. :func:`read_overview` / :func:`write_overview` — the cache, guarded by a
   fingerprint of the skeleton's inputs.

**Why the model call is not here.** The overview text is prose, so it is written
by a model — but on the ``dsh`` side, out of band (see ``dsh/src/overview.ts``),
never on the prompt-freeze path. The Python half only ever *aggregates* and
*caches*. That is what keeps the frozen snapshot byte-stable within a session and
keeps a model call off the hot path of every request: the freeze reads the cache,
and a missing cache degrades to the deterministic render rather than blocking on
a completion.

**Why two staleness signals instead of one.** They answer different questions and
neither substitutes for the other:

* :func:`change_level` answers *"is it worth generating again?"* It is a
  semantic judgement — twenty tweaked attributes do not change what the user has
  worked on, while one new decision rule does. Regenerating on any change would
  burn a model call per message.
* :func:`overview_fingerprint` answers *"does the cached text still describe this
  store?"* It is a cheap exact digest of the aggregation's inputs, and it is what
  stops a stale render whose change was only a *detail* (level ``detail``, no
  regeneration) from being served as if it were current.

Together they give the intended behaviour: detail-only changes neither trigger a
regeneration nor invalidate the cache, so the text keeps being served and stays
honest about what it was built from.
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
from typing import Dict, List, Mapping, Optional, Sequence, Tuple

from .config import MemConfig
from .context import GLOBAL_SCOPE_ID
from .db import now_ms
from .models import (
    NEUTRAL_SCORE,
    TYPE_DECISION_RULE,
    TYPE_EPISODIC,
    TYPE_FEW_SHOT,
    TYPE_LESSON,
    TYPE_PROCEDURAL,
    TYPE_SEMANTIC,
    TYPE_SOP,
    TYPE_TASK,
    default_importance,
)
from .reinforce import adjust, effective_importance
from .retriever import estimate_tokens
from .scope import ScopeStore

# -- change levels -------------------------------------------------------------
#
# Ordered by severity; a caller compares with the constants, never with a bare
# string, so a typo is an import error instead of a silently wrong threshold.
LEVEL_NONE = "none"
"""Nothing happened since the cached overview was written."""

LEVEL_DETAIL = "detail"
"""Only details moved: attributes, preferences, one-off events, reuse."""

LEVEL_STRUCTURAL = "structural"
"""The shape of the work changed: durable knowledge arrived, a new work unit or
topic appeared, or facts were retired."""

LEVEL_RESET = "reset"
"""The store was reorganised wholesale (purge, scope merge/split). Always
regenerates, and the cached text is treated as describing a different store."""

LEVEL_ORDER = {
    LEVEL_NONE: 0,
    LEVEL_DETAIL: 1,
    LEVEL_STRUCTURAL: 2,
    LEVEL_RESET: 3,
}


def level_at_least(level: str, threshold: str) -> bool:
    """Whether ``level`` is at or above ``threshold`` in severity.

    Args:
        level: An observed :data:`LEVEL_*` value.
        threshold: The level a caller acts on.

    Returns:
        ``True`` when the observed level is at least the threshold. Unknown
        values on either side rank as ``LEVEL_NONE``, so an unrecognised event
        type can never *raise* the level past a threshold by accident.
    """
    return LEVEL_ORDER.get(level, 0) >= LEVEL_ORDER.get(threshold, 0)


# Event types that mean "the store was reorganised", i.e. the overview's whole
# premise changed. Kept as one set because each one invalidates *every* work unit
# the cached text could be describing, which is a different claim from "something
# durable was added".
_RESET_EVENTS = frozenset({
    "facts_purged",
    "scope_merged",
    "scope_split",
    "scope_reparented",
    "domain_merged",
})

# Event types that mean "a durable kind of memory arrived or left". These are what
# make the overview worth rewriting: a decision rule, a lesson, a procedure or an
# open to-do each change what the user has been doing, whereas an attribute or a
# preference does not.
_STRUCTURAL_EVENTS = frozenset({
    "fact_superseded",
    "facts_archived",
    "scope_promoted",
    "scope_abstraction_promoted",
    "fact_scope_promoted",
    "domain_renamed",
})

# Memory types whose arrival is a structural change (see _STRUCTURAL_EVENTS).
# Deliberately excludes ``semantic`` (an attribute), ``preference`` (folded into
# semantic by the extractor) and ``episodic`` (a dated one-off): the extractor's
# own prompt calls episodic narration something *not* to record, so whatever
# trickles through must not trigger a rewrite.
_STRUCTURAL_TYPES = frozenset({
    TYPE_DECISION_RULE,
    TYPE_LESSON,
    TYPE_SOP,
    "procedural",
    "task",
    "few_shot",
})

#: How many changelog rows one classification reads. The classification is
#: "highest severity present", so the scan stops as soon as a structural or reset
#: event is seen; this is the cap on a window that is all details.
_CHANGE_SCAN_LIMIT = 500


def change_level(
    conn: sqlite3.Connection,
    user_id: str,
    since_ms: int,
) -> str:
    """Return how much the store moved for a user since ``since_ms``.

    The classification is deliberately **semantic rather than quantitative**: it
    asks *what kind* of thing changed, not how many rows did. A single new
    decision rule is :data:`LEVEL_STRUCTURAL`; fifty tweaked attributes are
    :data:`LEVEL_DETAIL`. Counting instead would make the trigger fire on exactly
    the busywork the cache exists to absorb.

    Reads the ``events`` changelog (migration 013's ``idx_events_user_created``
    makes it an index range scan, no sort). Facts written before that changelog
    existed contribute nothing, which is correct: the caller passes the
    ``updated_at`` of the cache it already holds, so an unwritten history is
    simply "no change observed".

    Args:
        conn: Open connection.
        user_id: Whose changelog to read.
        since_ms: Lower bound on ``events.created_at``, in ms, **inclusive**.

    Returns:
        One of the :data:`LEVEL_*` constants.
    """
    # The bound is inclusive (``>=``), not exclusive. Timestamps are whole
    # milliseconds, so a change written in the same millisecond as the cache —
    # which is the normal case for a fast refresh, and unavoidable in tests — is
    # indistinguishable from one written just before it. Excluding that boundary
    # made such a change invisible to the classifier, which then had to be caught
    # by the fingerprint fallback in :func:`should_refresh`: the same decision
    # reached by the wrong route, and in a real store the fingerprint does not
    # move at all for a structural write that leaves the row count, the scope set
    # and the timestamps untouched. Erring inclusive can only over-report change,
    # and over-reporting costs one regeneration while under-reporting silently
    # serves an overview that stopped describing the store.
    rows = conn.execute(
        "SELECT type, payload FROM events "
        "WHERE user_id = ? AND created_at >= ? "
        "ORDER BY created_at DESC LIMIT ?",
        (user_id, int(since_ms), _CHANGE_SCAN_LIMIT),
    ).fetchall()

    level = LEVEL_NONE
    for row in rows:
        event_type = str(row["type"] or "")
        if event_type in _RESET_EVENTS:
            return LEVEL_RESET
        if event_type in _STRUCTURAL_EVENTS:
            level = LEVEL_STRUCTURAL
            continue
        if event_type == "fact_written":
            if _written_type(row["payload"]) in _STRUCTURAL_TYPES:
                level = LEVEL_STRUCTURAL
            elif level == LEVEL_NONE:
                level = LEVEL_DETAIL
            continue
        # Anything else (fact_rejected, fact_deduplicated, task_dead, ...) is
        # activity, not a change to what the user has worked on.
        if level == LEVEL_NONE:
            level = LEVEL_DETAIL
    return level


def _written_type(payload: Optional[str]) -> str:
    """Return the memory ``type`` recorded on a ``fact_written`` event.

    Tolerant by design: the payload is JSON written by an earlier version of this
    code in a different process, so a malformed or missing payload must degrade
    to "an ordinary detail" rather than raise inside a changelog scan.
    """
    if not payload:
        return TYPE_SEMANTIC
    try:
        parsed = json.loads(payload)
    except (ValueError, TypeError):
        return TYPE_SEMANTIC
    if not isinstance(parsed, dict):
        return TYPE_SEMANTIC
    return str(parsed.get("type") or TYPE_SEMANTIC)


# -- the deterministic aggregation ---------------------------------------------


def build_overview_skeleton(
    conn: sqlite3.Connection,
    user_id: str,
    scope_context: Optional[Mapping] = None,
    config: Optional[MemConfig] = None,
    max_units: int = 8,
    highlights_per_unit: int = 3,
    budget_chars: int = 4000,
) -> dict:
    """Aggregate the store into the material a work overview is written from.

    Grouping is by **work unit** (the scope a fact is bound to), because that is
    what "what have I been working on" means. Facts bound to no scope are the
    global rules — durable guidance that applies everywhere — and are reported as
    their own unit rather than dropped.

    Every limit here exists to keep the result bounded: the overview text is
    written from this, so an unbounded skeleton would just move the token problem
    one step earlier. ``max_units`` caps the work units, ``highlights_per_unit``
    the examples per unit, and ``budget_chars`` a running character total across
    the whole skeleton.

    Args:
        conn: Open connection.
        user_id: Whose memory to aggregate.
        scope_context: The session's context payload, when there is one. It only
            orders the units (the session's own project first); it never filters
            them, because "what else have I done" must stay answerable.
        config: Configuration. ``None`` uses library defaults.
        max_units: Maximum work units returned.
        highlights_per_unit: Maximum example titles per unit.
        budget_chars: Running character cap over the whole skeleton.

    Returns:
        A JSON-serializable dict::

            {
              "units": [{"label", "type", "scope_id", "facts", "by_type",
                         "newest_at", "oldest_at", "highlights": [...]}],
              "topics": [{"label", "facts"}],
              "totals": {"facts", "units", "topics", "newest_at"},
              "truncated": bool,
              "fingerprint": "…",
            }

        Empty ``units`` and ``topics`` means the store holds nothing to narrate;
        callers treat that as "no overview", never as an error.
    """
    cfg = config or MemConfig()
    facts = _load_facts(conn, user_id)
    if not facts:
        return {
            "units": [],
            "topics": [],
            "totals": {"facts": 0, "units": 0, "topics": 0, "newest_at": 0},
            "truncated": False,
            "fingerprint": _fingerprint([], [], 0, 0, 0),
        }

    scope_of, labels = _scope_index(conn, user_id, facts, cfg)
    topic_of, topic_labels = _topic_index(conn, facts)

    units: Dict[str, dict] = {}
    for fact in facts:
        scope_ids = scope_of.get(fact["fact_id"]) or ()
        # A fact may be bound to several scopes; the primary grouping is the
        # first (fact_scopes orders by priority), so one fact is counted once.
        scope_id = next(
            (sid for sid in scope_ids if sid != GLOBAL_SCOPE_ID), GLOBAL_SCOPE_ID
        )
        key = str(scope_id)
        unit = units.get(key)
        if unit is None:
            unit = units[key] = {
                "scope_id": scope_id,
                "label": labels.get(scope_id, "全局"),
                "type": _scope_type(conn, scope_id, cfg),
                "facts": 0,
                "by_type": {},
                "newest_at": 0,
                "oldest_at": 0,
                "_candidates": [],
            }
        unit["facts"] += 1
        unit["by_type"][fact["type"]] = unit["by_type"].get(fact["type"], 0) + 1
        created = int(fact["created_at"] or 0)
        unit["newest_at"] = max(unit["newest_at"], created)
        unit["oldest_at"] = min(
            unit["oldest_at"] or created, created
        ) if unit["oldest_at"] else created
        unit["_candidates"].append(fact)

    ordered = _order_units(units, scope_context, cfg)
    budget = max(0, int(budget_chars))
    truncated = len(ordered) > max_units
    result_units: List[dict] = []
    for key in ordered[: max(0, int(max_units))]:
        unit = units[key]
        candidates = sorted(unit.pop("_candidates"), key=_fact_rank_key)
        highlights: List[str] = []
        for fact in candidates[: max(0, int(highlights_per_unit))]:
            title = _fact_title(fact)
            if not title:
                continue
            if budget - len(title) < 0:
                truncated = True
                break
            budget -= len(title)
            highlights.append(title)
        unit["highlights"] = highlights
        result_units.append(unit)

    topics: List[dict] = []
    counts: Dict[str, int] = {}
    for fact in facts:
        for label in topic_of.get(fact["fact_id"]) or ():
            counts[label] = counts.get(label, 0) + 1
    for label, count in sorted(counts.items(), key=lambda kv: (-kv[1], kv[0])):
        if budget - len(label) < 0:
            truncated = True
            break
        budget -= len(label)
        topics.append({"label": topic_labels.get(label, label), "facts": count})

    newest = max(int(fact["created_at"] or 0) for fact in facts)
    return {
        "units": result_units,
        "topics": topics,
        "totals": {
            "facts": len(facts),
            "units": len(units),
            "topics": len(counts),
            "newest_at": newest,
        },
        "truncated": truncated,
        "fingerprint": _fingerprint(
            [int(key) for key in units],
            sorted(counts),
            len(facts),
            newest,
            max(
                int(fact.get("last_used_at") or 0) for fact in facts
            ),
        ),
    }


def _load_facts(conn: sqlite3.Connection, user_id: str) -> List[dict]:
    """Load a user's active facts with the fields the aggregation reads.

    Only the columns the skeleton needs are selected: this runs on every
    out-of-band refresh, and a ``SELECT *`` would drag every knowledge body into
    memory for a view that never renders one.
    """
    rows = conn.execute(
        "SELECT fact_id, subject, predicate, object, type, created_at, "
        "last_used_at, importance, reinforce_count "
        "FROM facts WHERE user_id = ? AND status = 'active'",
        (user_id,),
    ).fetchall()
    facts: List[dict] = []
    for row in rows:
        fact = dict(row)
        fact["type"] = fact["type"] or TYPE_SEMANTIC
        stated = float(fact["importance"] or 0.0)
        rank = (
            stated
            if abs(stated - NEUTRAL_SCORE) > 1e-9
            else default_importance(fact["type"])
        )
        # The same "reuse strengthens rank" rule the summary and the retriever
        # apply, so a work unit's highlights agree with what a recall would rank
        # highest rather than being a third opinion.
        fact["rank"] = effective_importance(
            rank,
            adjust(
                float(fact.get("reinforce_count") or 0.0),
                fact.get("last_used_at"),
                now_ms(),
            ),
        )
        facts.append(fact)
    return facts


def _fact_rank_key(fact: dict) -> tuple:
    """Rank highlights by strength, then recency, then a stable id tiebreak."""
    return (-fact["rank"], -int(fact["created_at"] or 0), str(fact["fact_id"]))


def _fact_title(fact: dict) -> str:
    """Return a fact's one-line title, whitespace-normalised.

    Deliberately the same rule the compact digest uses (``summary._fact_title``)
    rather than a second one: the overview's highlights are meant to be
    recognisable as the same memories the detail views show.
    """
    value = str(fact.get("object") or "").strip()
    return " ".join(value.split())


def _scope_index(
    conn: sqlite3.Connection,
    user_id: str,
    facts: Sequence[dict],
    config: MemConfig,
) -> Tuple[Dict[str, Tuple[int, ...]], Dict[int, str]]:
    """Return ``({fact_id: (scope_id, ...)}, {scope_id: label})`` in two queries.

    Batched rather than per-fact: the aggregation walks every active fact, so a
    per-fact label lookup would be an N+1 on a path that runs on each refresh.
    """
    store = ScopeStore(conn, config)
    scope_of = store.fact_scopes([fact["fact_id"] for fact in facts])
    labels: Dict[int, str] = {}
    for scope_id in {sid for values in scope_of.values() for sid in values}:
        labels[scope_id] = store.label(scope_id)
    labels.setdefault(GLOBAL_SCOPE_ID, "全局")
    return scope_of, labels


def _scope_type(conn: sqlite3.Connection, scope_id: int, config: MemConfig) -> str:
    """Return a scope's type discriminator (``project`` / ``document`` / ...)."""
    if scope_id == GLOBAL_SCOPE_ID:
        return "global"
    store = ScopeStore(conn, config)
    try:
        return store.scope_type(scope_id)
    except Exception:  # pragma: no cover - defensive: a label we cannot type
        return "unknown"


def _topic_index(
    conn: sqlite3.Connection, facts: Sequence[dict]
) -> Tuple[Dict[str, List[str]], Dict[str, str]]:
    """Return ``({fact_id: [domain path, ...]}, {path: display name})``.

    The topic dimension is optional (``MemConfig.domain_recall`` defaults to
    ``off``), so this reads the tables directly and returns empty mappings when
    the migration that creates them has not run or the user has no labels. A
    missing topic dimension degrades the overview by omitting one line; it must
    never fail the aggregation.
    """
    fact_ids = [fact["fact_id"] for fact in facts]
    if not fact_ids:
        return {}, {}
    try:
        rows = conn.execute(
            "SELECT fd.fact_id AS fact_id, d.path AS path, d.display_name AS display_name "
            "FROM fact_domain fd JOIN domain d ON d.id = fd.domain_id "
            "WHERE fd.is_primary = 1"
        ).fetchall()
    except sqlite3.Error:  # pragma: no cover - pre-012 database
        return {}, {}
    wanted = set(fact_ids)
    out: Dict[str, List[str]] = {}
    names: Dict[str, str] = {}
    for row in rows:
        fact_id = str(row["fact_id"])
        if fact_id not in wanted:
            continue
        path = str(row["path"] or "")
        if not path:
            continue
        out.setdefault(fact_id, []).append(path)
        names.setdefault(path, str(row["display_name"] or path))
    return out, names


def _order_units(
    units: Mapping[str, dict],
    scope_context: Optional[Mapping],
    config: MemConfig,
) -> List[str]:
    """Order work units: the session's own project first, then most material.

    The session's scope is a *ordering* signal only, never a filter — the
    overview has to keep answering "and what else have I worked on", which a
    filter would silently reduce to the current project.

    Material comes before recency for the remainder: a project with forty facts
    has more to narrate than one touched yesterday with two, and the whole point
    of the overview is the shape of the work rather than the latest timestamp.
    """
    favoured: Optional[int] = None
    if scope_context:
        try:
            store_hint = scope_context.get("resolved_scope_id")
            if store_hint is not None:
                favoured = int(store_hint)
        except (AttributeError, TypeError, ValueError):  # pragma: no cover
            favoured = None

    def sort_key(key: str):
        unit = units[key]
        is_favoured = favoured is not None and unit["scope_id"] == favoured
        is_global = unit["scope_id"] == GLOBAL_SCOPE_ID
        # (favoured first, then non-global, then material, then recent, then id)
        return (
            0 if is_favoured else 1,
            1 if is_global else 0,
            -int(unit["facts"]),
            -int(unit["newest_at"] or 0),
            str(unit["scope_id"]),
        )

    return sorted(units, key=sort_key)


def resolve_current_scope_id(
    conn: sqlite3.Connection,
    config: MemConfig,
    scope_context: Mapping,
    user_id: str,
) -> Optional[int]:
    """Resolve a context payload to a scope id without creating anything.

    A read helper, so ``create=False`` semantics hold: aggregating an overview
    must never be the reason a scope comes into existence. Returns ``None`` when
    the context resolves to nothing, which the caller reads as "no unit is
    favoured" rather than as an error.
    """
    from .scope import resolution_for

    try:
        ctx, resolution = resolution_for(
            conn, config, scope_context, user_id=user_id, create=False
        )
    except Exception:  # pragma: no cover - defensive: a bad payload
        return None
    if ctx is None or resolution is None:
        return None
    return resolution.scope_id


# -- fingerprint ---------------------------------------------------------------


def overview_fingerprint(
    conn: sqlite3.Connection,
    user_id: str,
) -> str:
    """Return the digest of the store state an overview is built from.

    Read straight from the database rather than from a skeleton, because the
    fingerprint's job is to be cheap: the out-of-band job compares it *before*
    deciding whether to build anything, and a fingerprint that required the full
    aggregation first would make the check as expensive as the work it guards.

    Hashed inputs are exactly the ones the overview's text depends on: the active
    fact count, the newest write, the newest reuse, the set of bound scopes and
    the set of topic labels. ``last_used_at`` is included even though it does not
    change what has been worked on, because it does change *which* highlights a
    unit ranks first.

    Args:
        conn: Open connection.
        user_id: Whose store to digest.

    Returns:
        A 32-character hex digest. A store with no facts digests to a stable
        value (so "empty" is cached like anything else rather than recomputed).
    """
    row = conn.execute(
        "SELECT COUNT(*) AS n, "
        "COALESCE(MAX(created_at), 0) AS newest, "
        "COALESCE(MAX(last_used_at), 0) AS used "
        "FROM facts WHERE user_id = ? AND status = 'active'",
        (user_id,),
    ).fetchone()
    count = int(row["n"] or 0)
    if count == 0:
        return _fingerprint([], [], 0, 0, 0)
    scope_ids = [
        int(r["scope_id"])
        for r in conn.execute(
            "SELECT DISTINCT fs.scope_id FROM fact_scope fs "
            "JOIN facts f ON f.fact_id = fs.fact_id "
            "WHERE f.user_id = ? AND f.status = 'active'",
            (user_id,),
        ).fetchall()
    ]
    try:
        topics = [
            str(r["path"])
            for r in conn.execute(
                "SELECT DISTINCT d.path FROM fact_domain fd "
                "JOIN domain d ON d.id = fd.domain_id "
                "JOIN facts f ON f.fact_id = fd.fact_id "
                "WHERE f.user_id = ? AND f.status = 'active'",
                (user_id,),
            ).fetchall()
        ]
    except sqlite3.Error:  # pragma: no cover - pre-012 database
        topics = []
    return _fingerprint(
        scope_ids, sorted(topics), count, int(row["newest"] or 0), int(row["used"] or 0)
    )


def _fingerprint(
    scope_ids: Sequence[int],
    topics: Sequence[str],
    count: int,
    newest: int,
    used: int,
) -> str:
    """Hash the overview's inputs into a stable digest.

    Sorted before hashing, so the digest depends on the *set* of scopes and
    topics rather than on row order — a query plan change must not look like a
    store change.
    """
    payload = json.dumps(
        {
            "scopes": sorted(int(s) for s in scope_ids),
            "topics": sorted(str(t) for t in topics),
            "facts": int(count),
            "newest": int(newest),
            "used": int(used),
        },
        sort_keys=True,
        ensure_ascii=False,
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()[:32]


# -- the cache -----------------------------------------------------------------


def read_overview(
    conn: sqlite3.Connection,
    user_id: str,
    fingerprint: Optional[str] = None,
) -> Optional[str]:
    """Return the cached overview text, or ``None`` when there is no usable one.

    ``fingerprint`` is the guard that makes the cache honest: when it is given
    and does not match the stored row, the text describes a store that no longer
    exists and is treated as absent. Callers that pass it are the ones that can
    compute the current digest cheaply — the freeze path and the refresh job.

    Passing ``None`` returns whatever is cached, which is what the settings panel
    wants: a human inspecting the store should see the last generated text *and*
    be told it may be stale (:func:`overview_status`), rather than seeing nothing.

    Args:
        conn: Open connection.
        user_id: Whose cache to read.
        fingerprint: Expected current digest, or ``None`` to skip the check.

    Returns:
        The cached text, or ``None``.
    """
    row = conn.execute(
        "SELECT text, fingerprint FROM memory_overview WHERE user_id = ?",
        (user_id,),
    ).fetchone()
    if row is None:
        return None
    if fingerprint is not None and str(row["fingerprint"]) != str(fingerprint):
        return None
    text = str(row["text"] or "").strip()
    return text or None


def write_overview(
    conn: sqlite3.Connection,
    user_id: str,
    text: str,
    fingerprint: str,
    facts_count: int = 0,
    source: str = "llm",
) -> None:
    """Store (or replace) a user's cached overview.

    One row per user, upserted: the overview is a property of the whole store, so
    keeping a history of superseded texts would cost storage and prune nothing.
    The changelog already records *that* a refresh happened; this table only ever
    needs to answer "what is the current text".

    Empty text is refused rather than stored: a generation that produced nothing
    must leave the previous overview in place (the caller simply does not call
    this), because storing ``""`` would make the next freeze fall back to the
    deterministic render and lose an overview that was perfectly good.

    Args:
        conn: Open connection.
        user_id: Whose cache to write.
        text: The overview body. Whitespace-only input is ignored.
        fingerprint: The digest of the inputs this text was built from.
        facts_count: How many active facts it was built from (for display).
        source: ``llm`` for a model-written text, ``fallback`` for a
            deterministically rendered one, so the two are distinguishable.

    Returns:
        ``None``. A write failure is not raised: the overview is an optimisation
        over the deterministic render, and failing a caller's job over it would
        trade a nicer summary for a broken one.
    """
    body = (text or "").strip()
    if not body:
        return
    at = now_ms()
    try:
        with conn:
            conn.execute(
                "INSERT INTO memory_overview(user_id, text, fingerprint, "
                "facts_count, source, created_at, updated_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?) "
                "ON CONFLICT(user_id) DO UPDATE SET "
                "text = excluded.text, "
                "fingerprint = excluded.fingerprint, "
                "facts_count = excluded.facts_count, "
                "source = excluded.source, "
                "updated_at = excluded.updated_at",
                (
                    user_id,
                    body,
                    str(fingerprint),
                    int(facts_count),
                    str(source),
                    at,
                    at,
                ),
            )
    except sqlite3.Error:  # pragma: no cover - defensive
        import logging

        logging.getLogger(__name__).exception(
            "Failed to cache overview for %s", user_id
        )


def overview_status(conn: sqlite3.Connection, user_id: str) -> dict:
    """Report the cache state: what is stored, and whether it is still current.

    One payload answers the questions both consumers ask — the refresh job ("is
    it worth regenerating?") and the settings panel ("what is in there, and is it
    stale?"). Computing the current fingerprint here means a caller never has to
    make two round trips to decide.

    Args:
        conn: Open connection.
        user_id: Whose cache to inspect.

    Returns:
        ``{"cached", "fingerprint", "stale", "facts_count", "source",
        "updated_at", "level"}``. ``stale`` is ``True`` when there is no cache or
        when the stored digest no longer matches the store. ``level`` is the
        changelog level since the cache was written (``none`` with no cache).
    """
    row = conn.execute(
        "SELECT fingerprint, facts_count, source, updated_at, created_at "
        "FROM memory_overview WHERE user_id = ?",
        (user_id,),
    ).fetchone()
    current = overview_fingerprint(conn, user_id)
    if row is None:
        return {
            "cached": False,
            "fingerprint": current,
            "stale": True,
            "facts_count": 0,
            "source": None,
            "updated_at": 0,
            "level": LEVEL_NONE,
        }
    stored = str(row["fingerprint"])
    return {
        "cached": True,
        "fingerprint": stored,
        "current_fingerprint": current,
        "stale": stored != current,
        "facts_count": int(row["facts_count"] or 0),
        "source": str(row["source"] or ""),
        "updated_at": int(row["updated_at"] or 0),
        "created_at": int(row["created_at"] or 0),
        "level": change_level(conn, user_id, int(row["created_at"] or 0)),
    }


def should_refresh(
    conn: sqlite3.Connection,
    user_id: str,
    min_level: str = LEVEL_STRUCTURAL,
    min_facts: int = 1,
) -> Tuple[bool, str]:
    """Decide whether the cached overview is worth regenerating.

    This answers **"is it worth another model call"**, and that is deliberately a
    narrower question than "is the cached text still an exact match". The two are
    different, and conflating them was a real bug: the fingerprint moves on *any*
    fact write, so gating on it made every added attribute trigger a regeneration
    — exactly the busywork this gate exists to absorb.

    So the decision rests on the changelog level alone:

    * the changelog since the cache was written must reach ``min_level`` — the
      "a detail-only change does not move the overview" rule;
    * the store must hold at least ``min_facts`` facts — there is nothing to
      narrate in an empty store.

    A cache whose fingerprint has drifted at a lower level is left alone on
    purpose. The text still describes the same work; it is simply no longer an
    exact digest of the store, which is why :func:`overview_status` reports it as
    ``stale`` for a human and why :func:`read_overview` (given a fingerprint)
    refuses it. Serving a slightly-dated overview is the intended trade: the
    alternative is a model call per message to keep a summary of *what you have
    been working on* word-perfect about facts that did not change it.

    Args:
        conn: Open connection.
        user_id: Whose cache to judge.
        min_level: Lowest changelog level that justifies a regeneration.
        min_facts: Lowest active fact count that justifies one.

    Returns:
        ``(should, reason)`` where ``reason`` is a short machine-readable token
        (``no_facts`` / ``not_cached`` / ``level`` / ``up_to_date``) so the
        caller can log *why* it skipped without a second query.
    """
    row = conn.execute(
        "SELECT COUNT(*) AS n FROM facts WHERE user_id = ? AND status = 'active'",
        (user_id,),
    ).fetchone()
    if int(row["n"] or 0) < max(0, int(min_facts)):
        return False, "no_facts"

    cache = conn.execute(
        "SELECT created_at FROM memory_overview WHERE user_id = ?",
        (user_id,),
    ).fetchone()
    if cache is None:
        return True, "not_cached"

    level = change_level(conn, user_id, int(cache["created_at"] or 0))
    if level_at_least(level, min_level):
        return True, "level"
    return False, "up_to_date"


# -- changelog reads -----------------------------------------------------------


def recent_changes(
    conn: sqlite3.Connection,
    user_id: str,
    since_ms: int = 0,
    limit: int = 50,
) -> List[dict]:
    """Return a user's most recent memory changes, newest first.

    The human-facing half of the changelog: what the store did lately, in the
    order it did it. Only the events that describe a *change to stored memory*
    are returned — the audit log also carries repair and lifecycle noise
    (``task_dead``, ``index_repaired``) that is irrelevant to "what changed".

    Args:
        conn: Open connection.
        user_id: Whose changes to list.
        since_ms: Lower bound on ``created_at``, **inclusive**; ``0`` for all
            (which is why the default is 0 rather than "now minus something" —
            the boundary case is decided once, in :func:`change_level`).
        limit: Maximum rows.

    Returns:
        A list of ``{"type", "at", "detail": {...}}``, newest first. ``detail``
        is the decoded payload, trimmed to the keys worth showing; malformed
        payloads yield ``{}`` rather than raising.
    """
    rows = conn.execute(
        "SELECT type, payload, created_at FROM events "
        "WHERE user_id = ? AND created_at >= ? AND type IN "
        "('fact_written', 'fact_superseded', 'fact_rejected', "
        "'fact_deduplicated', 'facts_archived', 'facts_purged', "
        "'scope_created', 'scope_merged', 'scope_split', 'scope_reparented', "
        "'domain_registered', 'domain_merged', 'domain_renamed') "
        "ORDER BY created_at DESC LIMIT ?",
        (user_id, int(since_ms), max(1, int(limit))),
    ).fetchall()

    out: List[dict] = []
    for row in rows:
        payload: dict = {}
        try:
            parsed = json.loads(row["payload"] or "{}")
            if isinstance(parsed, dict):
                payload = parsed
        except (ValueError, TypeError):
            payload = {}
        detail = {
            key: payload[key]
            for key in (
                "type", "predicate", "subject", "object", "new_object",
                "old_object", "fact_id", "scope_id", "name", "reason",
            )
            if payload.get(key) not in (None, "")
        }
        out.append(
            {"type": str(row["type"]), "at": int(row["created_at"]), "detail": detail}
        )
    return out


# -- deterministic fallback render ---------------------------------------------


def render_skeleton_overview(
    skeleton: Mapping,
    max_chars: int = 1200,
    max_tokens: Optional[int] = None,
) -> str:
    """Render a skeleton as plain overview text, with no model involved.

    This is what the freeze path uses when the cache is empty — a deployment that
    never runs the out-of-band job, or a store whose first session starts before
    the job has ever fired. It is deliberately terse and structural rather than
    pretending to be prose: it says which work units exist, how much each holds
    and what its strongest entries are, which is a truthful smaller version of
    the same answer.

    ``max_tokens`` is applied **while rendering**, not by clipping afterwards, and
    that distinction matters at a small budget. :func:`estimate_tokens` charges
    CJK per character, so a fallback built to a character cap can be several times
    that in tokens; a caller that then had to shrink it would be cutting into a
    line it had already committed to. Deciding per line here lets the render stop
    while still whole — and let it shed progressively (highlights, then the
    breakdown, then the count) so that even a tiny budget gets the *names* of the
    work units rather than a severed sentence.

    Args:
        skeleton: A :func:`build_overview_skeleton` result.
        max_chars: Character cap on the rendered text.
        max_tokens: Optional token cap. ``None`` means "character cap only".

    Returns:
        The rendered text, or ``""`` when the skeleton holds no units.
    """
    units = list(skeleton.get("units") or [])
    if not units:
        return ""
    # Progressive detail: the most informative form is tried first and each
    # fallback says strictly less, so the render keeps whatever the budget allows.
    for detail in ("full", "no_highlights", "label_only"):
        text = _render_units(units, max_chars, detail, max_tokens)
        if text:
            return text
    return ""


def _render_units(
    units: Sequence[Mapping],
    max_chars: int,
    detail: str,
    max_tokens: Optional[int],
) -> str:
    """Render work units at one level of detail, stopping when a cap is met."""
    lines: List[str] = []
    spent = 0
    for unit in units:
        label = str(unit.get("label") or "全局")
        facts = int(unit.get("facts") or 0)
        if detail == "label_only":
            head = f"- {label}"
        else:
            breakdown = (
                _type_breakdown(unit.get("by_type") or {})
                if detail == "full"
                else ""
            )
            head = f"- {label}（{facts} 条{breakdown}）"
            if detail == "full":
                highlights = [str(h) for h in (unit.get("highlights") or []) if h]
                if highlights:
                    head += "：" + "；".join(highlights)
        if spent + len(head) > max_chars:
            break
        trial = lines + [head]
        if max_tokens is not None and estimate_tokens("\n".join(trial)) > max_tokens:
            break
        spent += len(head)
        lines.append(head)
    return "\n".join(lines)


def _type_breakdown(by_type: Mapping[str, int]) -> str:
    """Render ``{"decision_rule": 3}`` as `` · 决策规则 3``, strongest kind first."""
    if not by_type:
        return ""
    parts = [
        f"{_TYPE_LABELS.get(key, key)} {count}"
        for key, count in sorted(by_type.items(), key=_type_rank)
    ]
    return " · " + " · ".join(parts) if parts else ""


def _type_rank(item: Tuple[str, int]) -> tuple:
    """Sort key putting the durable kinds first, then by count, then by name.

    Ordering by :data:`_TYPE_ORDER` rather than by count: a unit's breakdown is
    read to see *what kind of work* it holds, and "决策规则 1 · 属性 9" is the
    informative order while "属性 9 · 决策规则 1" buries the durable part behind
    the noise. The count then orders within a kind, and the name breaks ties so
    the render is deterministic.
    """
    key, count = item
    try:
        rank = _TYPE_ORDER.index(str(key))
    except ValueError:
        rank = len(_TYPE_ORDER)
    return (rank, -int(count), str(key))


#: Display order for a work unit's type breakdown (durable first).
_TYPE_ORDER = [
    TYPE_DECISION_RULE,
    TYPE_LESSON,
    TYPE_SOP,
    TYPE_PROCEDURAL,
    TYPE_TASK,
    TYPE_FEW_SHOT,
    TYPE_SEMANTIC,
    "preference",
    TYPE_EPISODIC,
]


#: Short Chinese label per memory type, used when summarising a work unit.
#: Kept local to this module rather than imported from ``summary``: the two
#: render different things, and a shared constant would couple the overview's
#: wording to the digest's section titles for no benefit.
_TYPE_LABELS = {
    TYPE_DECISION_RULE: "决策规则",
    TYPE_LESSON: "教训",
    TYPE_SOP: "SOP",
    TYPE_PROCEDURAL: "流程",
    TYPE_TASK: "待办",
    TYPE_FEW_SHOT: "示例",
    TYPE_SEMANTIC: "属性",
    "preference": "偏好",
    TYPE_EPISODIC: "事件",
}


def skeleton_tokens(skeleton: Mapping) -> int:
    """Estimate the token cost of the skeleton rendered by
    :func:`render_skeleton_overview`, at the same budget arithmetic the summary
    renderer uses.

    Exposed because the summary renderer has to charge the overview section for
    its real cost before it decides how much detail fits underneath.
    """
    return estimate_tokens(render_skeleton_overview(skeleton))