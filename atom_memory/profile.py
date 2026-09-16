"""The user profile: an independent table the user owns.

The profile is **not** derived from active facts any more. It used to be a
projection that every read rebuilt, which made the settings panel's controls
partly fictional: a deleted row came back on the next read (the source fact was
still active), and a row whose fact had been retracted was never removed at all
(the projection only ever upserted). Both follow from the same thing — the table
was a cache, so the user's edits were addressed to the cache.

What replaces it:

- rows enter the profile only when the **user** accepts a generated suggestion
  or types one;
- rows leave it only when the **user** deletes one;
- facts are a *source of suggestions* (:func:`suggestible_profile_entries`),
  consumed by the LLM synthesis on the dsh side and shown to the user for
  approval — never written straight into the table.

This module owns the table's rules: what a valid row is, how many may exist,
and how the table renders for the model.
"""

from __future__ import annotations

import json
import sqlite3
from typing import Iterable, Optional

from .db import now_ms
from .retriever import estimate_tokens
from .validator import MULTI_VALUED_PREDICATES

# Provenance stored in `user_profile.source`. Distinct from the credibility
# source tags on facts: this one answers "where did the row come from", which
# decides nothing at runtime but tells the user which rows they wrote and which
# they accepted from a suggestion.
SOURCE_USER = "user"
SOURCE_GENERATED = "generated"

# Field caps for a stored row. A profile row is one line in the prompt, so a
# runaway value must not be able to push the render budget out on its own.
_MAX_SECTION_CHARS = 200
_MAX_KEY_CHARS = 200
_MAX_VALUE_CHARS = 2000


class ProfileLimitExceeded(Exception):
    """A write was refused because the profile is at its row cap.

    Carries the numbers so the caller can report a real limit rather than a
    generic failure — "50/50" is actionable, "write failed" is not.
    """

    def __init__(self, limit: int, current: int) -> None:
        self.limit = limit
        self.current = current
        super().__init__(
            f"用户画像已达上限（{current}/{limit} 条）：请先删除部分条目再新增"
        )


def profile_row_count(conn: sqlite3.Connection, user_id: str) -> int:
    """Return how many rows the user's profile currently holds."""
    return int(
        conn.execute(
            "SELECT COUNT(*) AS n FROM user_profile WHERE user_id = ?",
            (user_id,),
        ).fetchone()["n"]
    )


def _clean(text: object, limit: int) -> str:
    """Normalise one stored field: stripped, single-spaced, length-bounded."""
    return " ".join(str(text or "").split())[:limit]


def upsert_profile(
    conn: sqlite3.Connection,
    user_id: str,
    section: str,
    key: str,
    value: str,
    source: str = SOURCE_USER,
) -> bool:
    """Insert or update one profile row.

    Updating an *existing* row never counts against the cap (it does not add a
    row), so editing stays available even at the cap; only a genuinely new
    ``(section, key)`` can be refused, and the cap itself is enforced by the
    caller that knows the config (see :func:`write_profile_rows`).

    Args:
        conn: The SQLite connection.
        user_id: Owner of the profile.
        section: Profile section (a label like ``职业`` or ``偏好``).
        key: Key within the section (``value`` for simple rows).
        value: The stored value.
        source: ``user`` for a typed row, ``generated`` for an accepted
            suggestion.

    Returns:
        ``True`` when the row was written.
    """
    conn.execute(
        "INSERT INTO user_profile(user_id, section, key, value, source, "
        "confidence, privacy, updated_at) VALUES (?, ?, ?, ?, ?, 0.9, "
        "'private', ?) "
        "ON CONFLICT(user_id, section, key) DO UPDATE SET "
        "value = excluded.value, source = excluded.source, "
        "updated_at = excluded.updated_at",
        (user_id, section, key, value, source, now_ms()),
    )
    conn.commit()
    return True


def write_profile_rows(
    conn: sqlite3.Connection,
    user_id: str,
    rows: Iterable[dict],
    limit: int = 0,
    source: str = SOURCE_USER,
) -> dict:
    """Apply a batch of row edits (upserts and deletions) atomically.

    This is what the settings panel's one "save all" button drives. The cap is
    checked up front against the size the table *would* have, so a rejected
    batch leaves the table exactly as it was — no half-applied edit list, which
    is the failure mode that makes an editor untrustworthy.

    Args:
        conn: The SQLite connection.
        user_id: Owner of the profile.
        rows: Each row is ``{"section", "key", "value", "deleted"?}``.
        limit: Row cap; ``0`` disables it.
        source: Provenance tag for the rows this batch writes.

    Returns:
        ``{"written", "deleted"}`` counts.

    Raises:
        ProfileLimitExceeded: The batch would push the table past ``limit``.
        ValueError: A row is missing its section or key.
    """
    cleaned: list = []
    for row in rows:
        section = _clean(row.get("section"), _MAX_SECTION_CHARS)
        key = _clean(row.get("key"), _MAX_KEY_CHARS)
        value = _clean(row.get("value"), _MAX_VALUE_CHARS)
        deleted = bool(row.get("deleted"))
        # Every row — including a deletion — has to identify its target.
        if not section or not key:
            raise ValueError("profile section and key are required")
        cleaned.append((section, key, value, deleted))

    if limit > 0:
        existing = {
            (r["section"], r["key"])
            for r in conn.execute(
                "SELECT section, key FROM user_profile WHERE user_id = ?",
                (user_id,),
            ).fetchall()
        }
        projected = set(existing)
        for section, key, _value, deleted in cleaned:
            if deleted:
                projected.discard((section, key))
            else:
                projected.add((section, key))
        if len(projected) > limit:
            raise ProfileLimitExceeded(limit, len(projected))

    written = 0
    deleted_count = 0
    for section, key, value, deleted in cleaned:
        if deleted:
            deleted_count += int(delete_profile_row(conn, user_id, section, key))
        else:
            upsert_profile(conn, user_id, section, key, value, source=source)
            written += 1

    return {"written": written, "deleted": deleted_count}


def delete_profile_row(
    conn: sqlite3.Connection, user_id: str, section: str, key: str
) -> bool:
    """Delete one profile row.

    This is now a *real* delete: nothing re-derives the table, so the row stays
    gone. The underlying facts are untouched — the attribute is still remembered
    and still recallable, it is simply not a profile entry any more.

    Returns:
        ``True`` when a row was removed.
    """
    cur = conn.execute(
        "DELETE FROM user_profile WHERE user_id = ? AND section = ? AND key = ?",
        (user_id, section, key),
    )
    conn.commit()
    return cur.rowcount > 0


def list_profile_rows(conn: sqlite3.Connection, user_id: str) -> list:
    """Read the user's profile rows, ordered for display."""
    return conn.execute(
        "SELECT section, key, value, source, privacy, updated_at "
        "FROM user_profile WHERE user_id = ? ORDER BY section, key",
        (user_id,),
    ).fetchall()


def known_profile_keys(conn: sqlite3.Connection, user_id: str) -> set:
    """Return the ``(section, key)`` pairs already in the profile.

    The suggestion path excludes these, so a generation run never offers back
    something the user already has.
    """
    return {
        (r["section"], r["key"])
        for r in conn.execute(
            "SELECT section, key FROM user_profile WHERE user_id = ?",
            (user_id,),
        ).fetchall()
    }


def suggestible_profile_entries(conn: sqlite3.Connection, user_id: str) -> list:
    """Collect the profile entries the active facts *imply*, as suggestions.

    This is the original projection rule, reused as a suggestion source rather
    than as a writer: single-valued attributes imply ``(predicate, 'value')``
    and multi-valued preferences imply ``('偏好', object)``. Nothing is written
    to the table — the result is candidate material for the LLM synthesis, which
    the user then approves entry by entry.

    Returns:
        ``[{"section", "key", "value"}]``, de-duplicated by ``(section, key)``
        and ordered deterministically.
    """
    facts = conn.execute(
        "SELECT predicate, object, qualifiers, type FROM facts "
        "WHERE user_id = ? AND status = 'active'",
        (user_id,),
    ).fetchall()

    seen: set = set()
    out: list = []
    for f in facts:
        if (f["type"] or "semantic") != "semantic":
            continue
        predicate = f["predicate"]
        if predicate in MULTI_VALUED_PREDICATES:
            section = "偏好"
            key = _clean(f["object"], _MAX_KEY_CHARS)
            value = "不喜欢" if _negation(f["qualifiers"]) else "喜欢"
        else:
            section = _clean(predicate, _MAX_SECTION_CHARS)
            key = "value"
            value = _clean(f["object"], _MAX_VALUE_CHARS)
        if not section or not key or not value:
            continue
        if (section, key) in seen:
            continue
        seen.add((section, key))
        out.append({"section": section, "key": key, "value": value})
    out.sort(key=lambda r: (r["section"], r["key"]))
    return out


def profile_md(
    conn: sqlite3.Connection,
    user_id: str,
    max_tokens: int = 800,
) -> str:
    """Render the user's profile as markdown for ``user_md``.

    The rendering budget is a second, independent bound from the row cap: the
    table holds at most ``MemConfig.max_profile_rows`` rows, and this caps what
    those rows may cost in the prompt. Rows past the budget are left out *with a
    count*, so a truncated profile says so instead of looking complete.

    Args:
        conn: The SQLite connection.
        user_id: Owner of the profile.
        max_tokens: Estimated token budget for the body.

    Returns:
        Markdown string, or a notice when the profile is empty.
    """
    rows = list_profile_rows(conn, user_id)

    if not rows:
        return (
            f"# 用户画像 (User Profile) — {user_id}\n\n"
            "_暂无画像数据。_ (No profile data yet.)\n"
        )

    lines = [f"# 用户画像 (User Profile) — {user_id}", ""]
    budget = max_tokens
    rendered = 0
    for r in rows:
        line = _render_row(r)
        cost = estimate_tokens(line)
        if cost > budget:
            break
        lines.append(line)
        budget -= cost
        rendered += 1

    if rendered < len(rows):
        lines.append(f"（另有 {len(rows) - rendered} 条未展示）")
    return "\n".join(lines)


def _render_row(r) -> str:
    """Render one profile row, including its key when it is not 'value'."""
    if r["key"] == "value":
        body = f"**{r['section']}**: {r['value']}"
    else:
        body = f"**{r['section']}**: {r['key']} = {r['value']}"
    return f"- {body}"


def _negation(qualifiers: Optional[str]) -> bool:
    """Return whether a qualifiers JSON string carries a negation marker."""
    try:
        q = json.loads(qualifiers) if qualifiers else {}
    except (ValueError, TypeError):
        q = {}
    return bool(q.get("negation"))
