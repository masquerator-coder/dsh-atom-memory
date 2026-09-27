"""Detection of stored duplicates and conflicts that the write path did not catch.

The write path resolves identity *as writes arrive*: a restatement folds into the
row it matches (``Worker._fold_into``), and a contradiction under a single-valued
predicate supersedes the stored value. That is the right place for the decision,
but two facts that are duplicates *after the fact* are never looked at again — a
near-duplicate probe only examines candidates being written, and a conflict needs
a *new* assertion to trigger it.

This module supplies the missing read. It **reports only**; it never supersedes,
merges or deletes. Which of two stored claims the user meant is knowledge that
lives outside the store, so disposal belongs to the session model, through
``memory_replace`` / ``memory_forget``.

The one thing this module must get right is the definition of "conflict". A
predicate that may legitimately hold many objects — a preference, a to-do, a
lesson — is **not** in conflict with itself, however many objects it holds. That
question has exactly one authority in this codebase,
:func:`atom_memory.validator.is_multi_valued`, and it is consulted here rather
than re-derived. Skipping it produces a detector that reports every accumulated
list as a contradiction: measured against a real store, a naive
``(subject, predicate)`` + distinct-object rule returned 44 groups, and **all
44 were multi-valued memory**. A report that is entirely false positives is worse
than no report, because acting on it deletes legitimate memory.

Detection uses the data already in the table — fingerprints and
``(subject, predicate)`` grouping — deliberately. Finding reworded bodies would
mean re-embedding every stored fact, which is not a cost a background pass may
impose on a store of any real size.
"""

from __future__ import annotations

import sqlite3
from typing import Dict, List, Optional

from .config import MemConfig
from .models import TYPE_SEMANTIC
from .validator import is_multi_valued

# Report kinds. Stable strings: the host renders them and the tests assert them.
KIND_FINGERPRINT = "fingerprint"
KIND_SINGLE_VALUED_CONFLICT = "single_valued_conflict"


def _active_rows(conn: sqlite3.Connection, user_id: str) -> List[sqlite3.Row]:
    return conn.execute(
        "SELECT fact_id, subject, predicate, object, type, confidence, importance, "
        "created_at, content_fingerprint FROM facts "
        "WHERE user_id = ? AND status = 'active' ORDER BY created_at ASC",
        (user_id,),
    ).fetchall()


def detect_duplicates(conn: sqlite3.Connection, user_id: str) -> List[dict]:
    """Find active facts sharing a content fingerprint.

    The fingerprint is the store's own identity for a claim, so two active rows
    carrying one fingerprint are the same memory stored twice.

    Args:
        conn: Open connection.
        user_id: Whose facts to inspect.

    Returns:
        One entry per duplicated fingerprint, oldest fact first inside each.
    """
    groups: Dict[str, List[sqlite3.Row]] = {}
    for row in _active_rows(conn, user_id):
        fingerprint = row["content_fingerprint"]
        if fingerprint is None or str(fingerprint) == "":
            continue
        groups.setdefault(str(fingerprint), []).append(row)

    out: List[dict] = []
    for fingerprint, rows in groups.items():
        if len(rows) < 2:
            continue
        out.append(
            {
                "kind": KIND_FINGERPRINT,
                "key": fingerprint,
                "fact_ids": [str(r["fact_id"]) for r in rows],
                "detail": {
                    "subject": str(rows[0]["subject"]),
                    "predicate": str(rows[0]["predicate"]),
                    "object": str(rows[0]["object"]),
                },
            }
        )
    return out


def detect_conflicts(
    conn: sqlite3.Connection, user_id: str, config: Optional[MemConfig] = None
) -> List[dict]:
    """Find a single-valued predicate holding several active values.

    Only keys the validator calls **single-valued** are grouped. A multi-valued
    predicate is skipped entirely, whatever it holds: that is the difference
    between a report and a nuisance.

    Args:
        conn: Open connection.
        user_id: Whose facts to inspect.
        config: Deployment config, consulted for the extra multi-valued
            predicates a deployment registered. Omitting it only narrows the
            exemption; the built-in sets still apply inside ``is_multi_valued``.

    Returns:
        One entry per conflicting key, newest fact identified.
    """
    groups: Dict[tuple, List[sqlite3.Row]] = {}
    extra = tuple(getattr(config, "multi_valued_predicates", None) or ())
    for row in _active_rows(conn, user_id):
        predicate = str(row["predicate"] or "")
        # `memory_type` is required, not decorative: the type is the *primary*
        # multi-valued marker (a to-do, an episodic event, a knowledge item is
        # multi-valued whatever its predicate says), so omitting it reports every
        # task list as a conflict.
        if is_multi_valued(predicate, str(row["type"] or TYPE_SEMANTIC), extra):
            continue
        groups.setdefault((str(row["subject"] or ""), predicate), []).append(row)

    out: List[dict] = []
    for (subject, predicate), rows in groups.items():
        objects = {str(r["object"] or "") for r in rows}
        if len(objects) < 2:
            continue
        newest = max(rows, key=lambda r: (int(r["created_at"]), str(r["fact_id"])))
        out.append(
            {
                "kind": KIND_SINGLE_VALUED_CONFLICT,
                "key": f"{subject}/{predicate}",
                "fact_ids": [str(r["fact_id"]) for r in rows],
                "detail": {
                    "subject": subject,
                    "predicate": predicate,
                    "objects": sorted(objects),
                    "newest_fact_id": str(newest["fact_id"]),
                },
            }
        )
    return out


def build_report(
    conn: sqlite3.Connection, user_id: str, config: Optional[MemConfig] = None
) -> dict:
    """Build the full detection report for one owner.

    Args:
        conn: Open connection.
        user_id: Whose facts to inspect.
        config: Deployment config (see :func:`detect_conflicts`).

    Returns:
        ``{"duplicates": [...], "conflicts": [...], "counts": {...}}``.
    """
    duplicates = detect_duplicates(conn, user_id)
    conflicts = detect_conflicts(conn, user_id, config)
    return {
        "duplicates": duplicates,
        "conflicts": conflicts,
        "counts": {"duplicates": len(duplicates), "conflicts": len(conflicts)},
    }