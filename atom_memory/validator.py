"""Validation chain for atomic facts.

The validator applies a fixed sequence of checks to a candidate before it is
allowed into the ``facts`` (and derived FTS / vector) tables:

    empty -> degenerate -> confidence -> idempotency -> conflict -> privacy

Checks that need to look at existing data ("idempotency" and "conflict")
receive the live connection so they can query stored facts.
"""

from __future__ import annotations

import sqlite3
from typing import List, Optional, Sequence, Tuple

from .context import GLOBAL_SCOPE_ID
from .models import ALL_KNOWLEDGE, TYPE_TASK, FactCandidate, ValidationResult
from .sanitize import (
    DEFAULT_MAX_CONTENT_CHARS,
    DEFAULT_MAX_FIELD_CHARS,
    clean_body_meta,
    clean_field_meta,
    truncation_records,
)

# Allowed confidence range (inclusive).
_MIN_CONFIDENCE = 0.0
_MAX_CONFIDENCE = 1.0

# Privacy values considered acceptable; anything outside this set is rejected
# unless it matches the configured privacy filter.
_DEFAULT_PRIVACY = "private"

# Predicates that name a *preference* (a like, a dislike, a habit). This is the
# narrower of the two sets below, and it is the one the derived views route on:
# a preference renders as its own section and is projected into the user profile
# as "likes X". Multi-valuedness is a different question (see
# :data:`MULTI_VALUED_PREDICATES`) — a to-do list holds many items and is not a
# preference, so anything that needs "is this a preference" must ask here.
PREFERENCE_PREDICATES = {
    "偏好", "兴趣", "爱好", "喜欢", "不喜欢", "习惯", "擅长",
}

# Predicates that naturally hold several values at once. A different object
# under these predicates is an independent claim, not a contradiction.
# Everything not covered here — and not covered by the type/suffix tests in
# :func:`is_multi_valued` — is treated as a single-valued attribute whose object
# may only have one active value.
#
# The to-do family is here because a to-do list is a *collection*: the second
# to-do a user states is a new item, never a correction of the first, and
# judging it single-valued retires the earlier item (`newer_assertion`) or drops
# it inside the batch (`batch_duplicate`). The three " owns / teaches / works on
# many of these" relations below are the same failure measured on a live store:
# the predicate is one-to-many however the extractor happens to word it, and the
# predicates are *open vocabulary* (an LLM writes them), so this list is a
# fallback rather than the authority — the type marker (``TYPE_TASK``), the
# suffix rule and ``MemConfig.multi_valued_predicates`` carry the general case.
MULTI_VALUED_PREDICATES = PREFERENCE_PREDICATES | {
    "待办", "紧急待办", "待办事项", "任务", "下一步",
    "拥有项目", "教学课程", "日常工作线",
}

# Predicate *endings* that head a collection rather than naming one value
# ("课程大纲编写事项", "实验室采购清单"). Checked case-insensitively, after the
# exact sets above. Deliberately short, and deliberately not a general "looks
# plural" heuristic: a false positive only keeps two rows that a human can
# merge, while a false negative silently retires a memory — but an over-eager
# rule here would stop the store from ever answering with the *newest* value of
# a genuine single-valued attribute (a colour, a job title), which is the one
# behaviour the single-valued rule exists for.
MULTI_VALUED_PREDICATE_SUFFIXES = (
    "事项", "任务", "清单", "待办", "列表", "条目", "todo",
)

# ---- degenerate-fact filtering -------------------------------------------------
#
# When a descriptive sentence *about the system itself* reaches the extractor it
# can yield a fact whose object merely echoes its own predicate
# ("被谁调用 -> 被调用的对象", "起到的作用 -> 起到的作用"). Such facts carry no
# information, yet they are persisted and then pollute the rendered summary and
# ``memory.md``. They are rejected as ``degenerate``.

# Question words removed from a predicate to obtain its "core".
_INTERROGATIVES = (
    "什么时候", "什么样", "为什么", "怎么样",
    "什么", "哪儿", "哪里", "何时", "如何", "怎么", "多少", "是否",
    "谁", "啥", "哪", "几",
)

# Generic head nouns. An object that *both* reuses the predicate core and ends
# with one of these is a placeholder, not a real value.
_GENERIC_HEADS = (
    "对象", "作用", "东西", "内容", "信息", "事情", "情况", "方面",
    "角色", "功能", "人员", "相关方", "值",
)

# Whole-object placeholders rejected outright (normalized to lower case).
_PLACEHOLDER_OBJECTS = frozenset({
    "起到的作用", "被调用的对象", "被调用者", "调用者", "对象", "作用",
    "东西", "内容", "信息", "事情", "情况", "方面", "功能", "角色",
    "未知", "待定", "不详", "其他", "其它", "无", "n/a", "na", "none", "null",
})

# Minimum share of the object length that the predicate core must cover for the
# echo heuristic to fire. Guards against false positives such as
# "角色: 项目经理的角色" (core 2 chars / object 7 chars = 0.29 -> kept).
_ECHO_MIN_RATIO = 0.5

# Punctuation/whitespace trimmed from both ends during normalization.
_TRIM_CHARS = " \t\r\n。．.！！?？；;，,、：:"


def validate(
    candidate: FactCandidate,
    conn: Optional[sqlite3.Connection] = None,
    privacy_filter: str = _DEFAULT_PRIVACY,
    max_field_chars: int = DEFAULT_MAX_FIELD_CHARS,
    max_content_chars: int = DEFAULT_MAX_CONTENT_CHARS,
    scope_ids: Optional[Sequence[int]] = None,
    multi_valued_predicates: Optional[Sequence[str]] = None,
) -> ValidationResult:
    """Run the full validation chain on a candidate.

    The chain starts by **normalising the candidate in place** (whitespace and
    invisible-character removal, field/body length caps). That is deliberate:
    this function is the single gate every fact write passes through, so
    cleaning here is what guarantees the store never holds a value that carries
    hidden instructions or an unbounded field — and it means the later checks
    judge exactly the text that will be stored, not the raw text.

    Args:
        candidate: The candidate to validate (normalised in place).
        conn: Optional connection used for idempotency and conflict lookups.
            If ``None`` those checks short-circuit to passing (write-time
            enforcement still applies via the DB constraints).
        privacy_filter: Configured privacy tag; if ``candidate.privacy`` does
            not match it, the ``privacy`` check fails.
        max_field_chars: Ingest cap for one SPO field.
        max_content_chars: Ingest cap for a knowledge body.
        scope_ids: Scopes the candidate is being written into (the resolved
            scope plus its ancestors, or ``None`` for a scope-blind write). When
            given, the conflict check only considers stored facts visible from
            those scopes, so a project's statement is not judged contradictory
            against another project's — the design's "跨作用域只建链接或标参考".
        multi_valued_predicates: Deployment-declared multi-valued predicates
            (``MemConfig.multi_valued_predicates``), on top of the built-in set.

    Returns:
        A :class:`ValidationResult` describing the outcome.
    """
    # 0. Normalise (not a check: it decides what the checks look at). The
    #    truncation records are attached to whichever result is returned, so the
    #    caller can report a shortened write even when it was also rejected.
    truncated = normalize_candidate_meta(
        candidate, max_field_chars=max_field_chars, max_content_chars=max_content_chars
    )
    result = _validate_checks(
        candidate, conn, privacy_filter, scope_ids, multi_valued_predicates
    )
    result.truncated_fields = truncated
    return result


def _validate_checks(
    candidate: FactCandidate,
    conn: Optional[sqlite3.Connection],
    privacy_filter: str,
    scope_ids: Optional[Sequence[int]] = None,
    multi_valued_predicates: Optional[Sequence[str]] = None,
) -> ValidationResult:
    """Run the check chain on a candidate that has already been normalised.

    Args:
        candidate: The normalised candidate.
        conn: Optional connection for the idempotency/conflict lookups.
        privacy_filter: Configured privacy tag.
        scope_ids: Scopes the write targets (see :func:`validate`).
        multi_valued_predicates: Extra multi-valued predicates (see
            :func:`validate`).

    Returns:
        The first failing result, or a passing one.
    """
    # 1. Empty
    empty = _check_empty(candidate)
    if not empty.ok:
        return empty

    # 2. Degenerate (placeholder / predicate-echo objects)
    degen = _check_degenerate(candidate)
    if not degen.ok:
        return degen

    # 3. Confidence
    conf = _check_confidence(candidate)
    if not conf.ok:
        return conf

    # 4. Idempotency
    idem = _check_idempotency(candidate, conn)
    if not idem.ok:
        return idem

    # 5. Conflict
    conflict = _check_conflict(candidate, conn, scope_ids, multi_valued_predicates)
    if not conflict.ok:
        return conflict

    # 6. Privacy
    priv = _check_privacy(candidate, privacy_filter)
    if not priv.ok:
        return priv

    return ValidationResult.pass_(candidate.candidate_id)


def normalize_candidate_meta(
    candidate: FactCandidate,
    max_field_chars: int = DEFAULT_MAX_FIELD_CHARS,
    max_content_chars: int = DEFAULT_MAX_CONTENT_CHARS,
) -> List[dict]:
    """Clean a candidate's text fields in place and report what was shortened.

    ``normalize_candidate`` is the silent form (text only). This one exists
    because capping is the single normalisation step that *loses* information:
    a caller that stores the result has to be able to tell the user "kept the
    first 20 000 characters" instead of quietly answering questions about text
    it no longer has.

    Args:
        candidate: The candidate to normalise.
        max_field_chars: Cap for one SPO field.
        max_content_chars: Cap for the knowledge body.

    Returns:
        One record per field that had to be capped (``field``,
        ``original_chars``, ``kept_chars``); empty when nothing was lost.
    """
    records: List[dict] = []
    for name in ("subject", "predicate", "object"):
        value = getattr(candidate, name, None)
        if value is None:
            continue
        cleaned = clean_field_meta(value, max_field_chars)
        setattr(candidate, name, cleaned.text)
        records.append(cleaned.record(name))
    if candidate.content is not None:
        cleaned = clean_body_meta(candidate.content, max_content_chars)
        candidate.content = cleaned.text or None
        records.append(cleaned.record("content"))
    return truncation_records(records)


def normalize_candidate(
    candidate: FactCandidate,
    max_field_chars: int = DEFAULT_MAX_FIELD_CHARS,
    max_content_chars: int = DEFAULT_MAX_CONTENT_CHARS,
) -> FactCandidate:
    """Clean a candidate's text fields in place and return it.

    Subject / predicate / object become single-line, invisible-character-free
    and length-capped; content keeps its line structure but loses its invisible
    characters and is length-capped. ``qualifiers`` is left alone: it is JSON,
    and mangling it would corrupt the structured fields (``negation``, ``when``,
    ``steps``) the rest of the library reads from it.

    Args:
        candidate: The candidate to normalise.
        max_field_chars: Cap for one SPO field.
        max_content_chars: Cap for the knowledge body.

    Returns:
        The same candidate object, normalised.
    """
    normalize_candidate_meta(
        candidate, max_field_chars=max_field_chars, max_content_chars=max_content_chars
    )
    return candidate


def _check_empty(candidate: FactCandidate) -> ValidationResult:
    """Require a non-blank subject, predicate and object."""
    missing = [
        field
        for field, value in (
            ("subject", candidate.subject),
            ("predicate", candidate.predicate),
            ("object", candidate.object),
        )
        if not (value and str(value).strip())
    ]
    if missing:
        return ValidationResult.fail(
            "empty",
            f"required fields empty: {', '.join(missing)}",
            candidate.candidate_id,
        )
    return ValidationResult.pass_(candidate.candidate_id)


def _check_degenerate(candidate: FactCandidate) -> ValidationResult:
    """Reject a candidate whose object carries no information.

    Three independent rules, applied to the normalized subject / predicate /
    object:

    A. the object merely repeats the subject or the predicate
       ("起到的作用 -> 起到的作用");
    B. the object is a known placeholder phrase ("对象", "待定", ...);
    C. the object reuses the predicate core (the predicate with its question
       words removed) *and* ends with a generic head noun, while covering at
       least :data:`_ECHO_MIN_RATIO` of the object
       ("被谁调用 -> 被调用的对象").

    The length guard in C keeps informative objects such as
    "角色: 项目经理的角色" alive.
    """
    subject = _normalize(candidate.subject)
    predicate = _normalize(candidate.predicate)
    obj = _normalize(candidate.object)

    # A. Object echoes the subject or the predicate.
    if obj and obj in (subject, predicate):
        echoed = "subject" if obj == subject else "predicate"
        return ValidationResult.fail(
            "degenerate",
            f"object '{candidate.object}' only echoes the {echoed}",
            candidate.candidate_id,
        )

    # B. Object is a placeholder phrase.
    if obj in _PLACEHOLDER_OBJECTS:
        return ValidationResult.fail(
            "degenerate",
            f"object '{candidate.object}' is a placeholder with no content",
            candidate.candidate_id,
        )

    # C. Object reuses the predicate core and ends with a generic head noun.
    core = _predicate_core(predicate)
    if (
        core
        and core in obj
        and obj.endswith(_GENERIC_HEADS)
        and len(core) >= _ECHO_MIN_RATIO * len(obj)
    ):
        return ValidationResult.fail(
            "degenerate",
            f"object '{candidate.object}' restates the predicate "
            f"'{candidate.predicate}' as a placeholder",
            candidate.candidate_id,
        )

    return ValidationResult.pass_(candidate.candidate_id)


def _normalize(value: Optional[str]) -> str:
    """Lower-case, collapse whitespace and trim surrounding punctuation."""
    text = " ".join(str(value or "").split())
    text = text.strip(_TRIM_CHARS)
    # Drop a leading/trailing possessive particle ("的的作用" -> "作用").
    while len(text) > 1 and text.startswith("的"):
        text = text[1:]
    while len(text) > 1 and text.endswith("的"):
        text = text[:-1]
    return text.lower()


def _predicate_core(predicate: str) -> str:
    """Return the predicate with its question words removed.

    Longer question words are removed first so that "哪里" is not reduced to a
    stray "里" by the shorter "哪".
    """
    core = predicate
    for word in sorted(_INTERROGATIVES, key=len, reverse=True):
        core = core.replace(word, "")
    return core.strip(_TRIM_CHARS).strip()


def _check_confidence(candidate: FactCandidate) -> ValidationResult:
    """Require confidence to lie within the allowed range."""
    c = float(candidate.confidence)
    if not (_MIN_CONFIDENCE <= c <= _MAX_CONFIDENCE):
        return ValidationResult.fail(
            "confidence",
            f"confidence {c} out of range [{_MIN_CONFIDENCE}, {_MAX_CONFIDENCE}]",
            candidate.candidate_id,
        )
    return ValidationResult.pass_(candidate.candidate_id)


def _check_idempotency(
    candidate: FactCandidate, conn: Optional[sqlite3.Connection]
) -> ValidationResult:
    """Suppress a candidate whose idempotency key is already recorded.

    The UNIQUE constraint on ``fact_candidates.idempotency_key`` enforces this
    at write time; this check gives a friendly early signal. When the key is
    absent the check passes (no dedup requested).
    """
    key = candidate.idempotency_key
    if not key or conn is None:
        return ValidationResult.pass_(candidate.candidate_id)

    row = conn.execute(
        "SELECT candidate_id FROM fact_candidates WHERE idempotency_key = ?",
        (key,),
    ).fetchone()
    if row is not None:
        # ``suppressed``'s contract is "the existing *fact_id* that already
        # represents this candidate". The prior row is a *candidate*, not a
        # fact, but when that claim has since been applied to an active fact of
        # the same owner + SPO, handing back the real ``fact_id`` lets the
        # worker record the reuse-reinforcement (see worker._process_extract):
        # restating an already-stored claim is the cleanest reuse evidence the
        # library can observe, and this idempotency-key dedup fires *before* the
        # same-SPO path in ``_check_conflict``, so without this the signal would
        # never land. When no such active fact exists (the prior candidate was
        # skipped, or the fact was retracted) there is nothing to reinforce, and
        # ``suppressed`` is left unset for a pure suppress — never a candidate id
        # masquerading as a fact id.
        fact = conn.execute(
            "SELECT fact_id FROM facts WHERE user_id = ? AND subject = ? "
            "AND predicate = ? AND object = ? AND status = 'active' "
            "ORDER BY created_at ASC LIMIT 1",
            (
                candidate.user_id,
                candidate.subject,
                candidate.predicate,
                candidate.object,
            ),
        ).fetchone()
        return ValidationResult.fail(
            "idempotent",
            f"idempotency_key already used by candidate {row['candidate_id']}",
            candidate.candidate_id,
            suppressed=fact["fact_id"] if fact is not None else None,
        )
    return ValidationResult.pass_(candidate.candidate_id)


def is_multi_valued(
    predicate: str,
    memory_type: str,
    extra_predicates: Optional[Sequence[str]] = None,
) -> bool:
    """Whether an SPO (predicate, type) may legitimately hold many objects.

    A predicate is multi-valued when

    * its **type** says so — a to-do (``TYPE_TASK``), an episodic event, or a
      knowledge item (``ALL_KNOWLEDGE``). The type is the primary marker because
      it is a decision the extractor makes about the *claim*, while the
      predicate is free text it invents;
    * its **predicate** is a known collection predicate
      (``MULTI_VALUED_PREDICATES``, or a deployment's
      ``MemConfig.multi_valued_predicates``);
    * or its predicate **ends with a collection head noun**
      (``MULTI_VALUED_PREDICATE_SUFFIXES``).

    Under any of these a *different* object is an independent claim, not a
    contradiction. Everything else is a single-valued attribute whose object may
    only have one active value.

    This is the single authority for the rule, shared by the write-path batch
    dedup (:meth:`atom_memory.worker.Worker._dedupe_batch`), the write-path
    conflict check (:func:`_check_conflict`) and the read-path conflict report
    (:func:`atom_memory.api.AtomMem.recall`), so all of them stay in agreement.

    Args:
        predicate: The stored/extracted predicate.
        memory_type: The memory type discriminator of the same claim.
        extra_predicates: Additional predicates a deployment declared
            multi-valued (``MemConfig.multi_valued_predicates``).

    Returns:
        ``True`` when the key may hold several active values at once.
    """
    if memory_type == TYPE_TASK:
        return True
    if memory_type == "episodic" or predicate == "事件":
        return True
    if memory_type in ALL_KNOWLEDGE:
        return True
    if predicate in MULTI_VALUED_PREDICATES:
        return True
    if extra_predicates and predicate in extra_predicates:
        return True
    if predicate and str(predicate).strip().lower().endswith(
        MULTI_VALUED_PREDICATE_SUFFIXES
    ):
        return True
    return False


def _scope_clause(
    scope_ids: Optional[Sequence[int]], alias: str = "facts"
) -> Tuple[str, list]:
    """Build the SQL fragment restricting a fact query to a scope set.

    An unbound fact counts as global (the compatibility rule migration 011
    establishes), so it is visible whenever the root is in ``scope_ids`` — which
    it always is, because resolution returns a scope path that ends at
    ``/global``.

    Args:
        scope_ids: The scopes a write targets, or ``None`` for no restriction.
        alias: Table alias the fragment must reference.

    Returns:
        ``(sql, params)`` where ``sql`` is either empty or an ``AND (...)``
        fragment, so callers can append it unconditionally.
    """
    if scope_ids is None:
        return "", []
    ids = sorted({int(s) for s in scope_ids})
    if not ids:
        return "", []
    placeholders = ",".join("?" for _ in ids)
    unbound = ""
    if GLOBAL_SCOPE_ID in ids:
        unbound = (
            f" OR NOT EXISTS (SELECT 1 FROM fact_scope fsu "
            f"WHERE fsu.fact_id = {alias}.fact_id)"
        )
    sql = (
        f"AND (EXISTS (SELECT 1 FROM fact_scope fs WHERE fs.fact_id = {alias}.fact_id "
        f"AND fs.scope_id IN ({placeholders})){unbound}) "
    )
    return sql, ids


def cross_scope_neighbours(
    candidate: FactCandidate,
    conn: Optional[sqlite3.Connection],
    scope_ids: Optional[Sequence[int]],
) -> List[dict]:
    """Return active facts under the same key that live in *other* scopes.

    The design does not let a cross-scope collision be judged a contradiction: a
    project that overrides a global rule is not evidence that the rule is wrong,
    and picking a winner silently loses either the rule or its exception. So the
    write path keeps both and records a relation instead — which needs to know
    which facts are neighbours but out of scope.

    Args:
        candidate: The candidate being written.
        conn: Open connection, or ``None``.
        scope_ids: The scope the write targets. ``None`` (a scope-blind write)
            returns nothing: without a scope there is no "other" scope.

    Returns:
        Rows (with ``fact_id``, ``object``, ``qualifiers``, ``confidence``,
        ``importance``, ``created_at``) under the same user + subject +
        predicate that hold a binding outside ``scope_ids``, plus unbound facts
        whenever the root is not in ``scope_ids`` — an unbound fact is a global
        fact by the compatibility rule, so a project-scoped write must still see
        it as a neighbour from another scope.
    """
    if conn is None or scope_ids is None or not candidate.subject or not candidate.predicate:
        return []
    ids = sorted({int(s) for s in scope_ids})
    if not ids:
        return []
    placeholders = ",".join("?" for _ in ids)
    unbound = ""
    if GLOBAL_SCOPE_ID not in ids:
        unbound = (
            " OR NOT EXISTS (SELECT 1 FROM fact_scope fsu WHERE fsu.fact_id = f.fact_id)"
        )
    rows = conn.execute(
        f"SELECT f.fact_id, f.object, f.qualifiers, f.confidence, f.importance, "
        f"f.created_at FROM facts f "
        f"WHERE f.user_id = ? AND f.subject = ? AND f.predicate = ? "
        f"AND f.status = 'active' "
        f"AND (EXISTS (SELECT 1 FROM fact_scope fs WHERE fs.fact_id = f.fact_id "
        f"AND fs.scope_id NOT IN ({placeholders})){unbound}) "
        f"ORDER BY f.created_at DESC",
        [candidate.user_id, candidate.subject, candidate.predicate, *ids],
    ).fetchall()
    return [dict(row) for row in rows]


def _check_conflict(
    candidate: FactCandidate,
    conn: Optional[sqlite3.Connection],
    scope_ids: Optional[Sequence[int]] = None,
    multi_valued_predicates: Optional[Sequence[str]] = None,
) -> ValidationResult:
    """Detect a contradiction against an existing active fact.

    The comparison runs in **two passes over the same, deterministically ordered
    row set**, because the answer must not depend on which row SQLite happens to
    return first:

    - **Pass 1 — is this claim already stored?** Any active row with the same
      object decides the outcome: same negation is a restatement
      (``idempotent``), a flipped negation is a direct contradiction
      (``conflict`` against *that* row).
    - **Pass 2 — a different object under the same key.** Multi-valued
      predicates, knowledge items, to-dos and episodic events hold many objects,
      so this is an independent claim, not a conflict. Under a single-valued
      predicate it is a contradiction against *every* active value under the key,
      and all of those rows are handed back in ``conflict_rows`` (newest first)
      so the write path can resolve the whole key rather than one arbitrary row.

    Inactive (e.g. ``superseded`` / ``retracted`` / ``archived``) facts are
    ignored throughout.

    When ``scope_ids`` is given, only facts *visible from those scopes* take
    part: the candidate's own scope, its ancestors and the root. A claim stored
    under a sibling project is therefore not a contradiction — it is a
    cross-scope neighbour, which :func:`cross_scope_neighbours` reports
    separately so the write path can link the two instead of retiring one.
    """
    if conn is None or not candidate.subject or not candidate.predicate:
        return ValidationResult.pass_(candidate.candidate_id)

    cand_obj = (candidate.object or "").strip()
    cand_neg = _has_negation(candidate.qualifiers)
    cand_type = getattr(candidate, "type", "semantic") or "semantic"
    # Knowledge items stay independent under a shared predicate: a second SOP or
    # lesson is a new item, not a contradiction of the first.
    multi_valued = is_multi_valued(
        candidate.predicate, cand_type, multi_valued_predicates
    )

    scope_sql, scope_args = _scope_clause(scope_ids, alias="f")
    rows = conn.execute(
        "SELECT f.fact_id, f.object, f.qualifiers, f.confidence, f.importance, "
        "f.created_at "
        "FROM facts f "
        "WHERE f.user_id = ? AND f.subject = ? AND f.predicate = ? "
        "AND f.status = 'active' "
        + scope_sql
        + "ORDER BY f.created_at DESC, f.rowid DESC",
        [candidate.user_id, candidate.subject, candidate.predicate, *scope_args],
    ).fetchall()

    # -- Pass 1: the claim itself ---------------------------------------------
    for row in rows:
        if (row["object"] or "").strip() != cand_obj:
            continue
        if _has_negation(row["qualifiers"]) == cand_neg:
            return ValidationResult.fail(
                "idempotent",
                f"identical active fact already exists: {row['fact_id']}",
                candidate.candidate_id,
                suppressed=row["fact_id"],
            )
        return ValidationResult.fail(
            "conflict",
            f"negation contradicts active fact {row['fact_id']}",
            candidate.candidate_id,
            conflict_with=row["fact_id"],
            conflict_rows=[dict(row)],
        )

    # -- Pass 2: a different object under the same key -------------------------
    if multi_valued or not rows:
        return ValidationResult.pass_(candidate.candidate_id)

    conflicting = [dict(row) for row in rows]
    objects = ", ".join(repr(r["object"]) for r in conflicting[:3])
    return ValidationResult.fail(
        "conflict",
        f"{candidate.predicate!r} is single-valued but holds "
        f"{len(conflicting)} active value(s): {objects}",
        candidate.candidate_id,
        conflict_with=conflicting[0]["fact_id"],
        conflict_rows=conflicting,
    )


def has_negation(qualifiers: Optional[str]) -> bool:
    """Public form of the negation test, for callers that need claim polarity.

    The content fingerprint has to include polarity (see
    :mod:`~atom_memory.fingerprint`), so the rule for "is this claim negated"
    lives in one place and is importable rather than duplicated.

    Args:
        qualifiers: The candidate's/fact's raw ``qualifiers`` JSON, if any.

    Returns:
        Whether the claim carries a negation.
    """
    return _has_negation(qualifiers)


def _has_negation(qualifiers: Optional[str]) -> bool:
    """Return whether a qualifiers JSON string carries a negation marker."""
    import json

    try:
        q = json.loads(qualifiers) if qualifiers else {}
    except (ValueError, TypeError):
        q = {}
    return bool(q.get("negation"))


def _check_privacy(
    candidate: FactCandidate, privacy_filter: str
) -> ValidationResult:
    """Apply / validate the privacy tag.

    A blank privacy tag is filled with the configured filter; a non-blank tag
    that disagrees with the configured filter is rejected.
    """
    tag = (candidate.privacy or "").strip() or privacy_filter
    if tag != privacy_filter:
        return ValidationResult.fail(
            "privacy",
            f"privacy '{tag}' does not match configured filter '{privacy_filter}'",
            candidate.candidate_id,
        )
    candidate.privacy = tag
    return ValidationResult.pass_(candidate.candidate_id)
