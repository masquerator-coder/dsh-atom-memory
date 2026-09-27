"""Tests for stored duplicate/conflict detection.

The detection passes are the read-side counterpart to the write path's identity
resolution: they find what the write path could not, because a duplicate or a
contradiction that only appears *after* the fact was stored has no write to
trigger on.

The definition of "conflict" is the part that has to be right. A predicate that
may legitimately hold many objects — a preference, a to-do, a lesson — is not in
conflict with itself, and this suite pins that with the shape a real store has.
"""

from __future__ import annotations

from atom_memory import MemConfig
from atom_memory.consolidate import build_report
from atom_memory.db import connect_for_tests, now_ms


def _insert(conn, fact_id, subject, predicate, obj, *, memory_type="semantic",
            created_at=None, fingerprint=None):
    """Seed one active fact directly.

    The detection passes read the table, so seeding it directly keeps these tests
    independent of extraction, embedding and scope resolution — and lets a test
    set the `type` that `is_multi_valued` consults.
    """
    ts = created_at if created_at is not None else now_ms()
    conn.execute(
        "INSERT INTO facts(fact_id, user_id, session_id, subject, predicate, object, "
        "confidence, importance, privacy, source_type, status, observed_at, created_at, "
        "version, type, content_fingerprint) "
        "VALUES (?, 'u', 's1', ?, ?, ?, 0.5, 0.5, 'private', 'user_explicit', 'active', "
        "?, ?, 1, ?, ?)",
        (fact_id, subject, predicate, obj, ts, ts, memory_type, fingerprint),
    )
    conn.commit()


def test_a_multi_valued_predicate_is_never_reported_as_a_conflict():
    """The defect that made the first draft 100% wrong on real data.

    Nineteen coexisting preferences under one predicate are nineteen memories,
    not a contradiction. `is_multi_valued` is the project's authority on that,
    and the detector must consult it rather than assuming one object per key.
    """
    conn = connect_for_tests(MemConfig())
    try:
        for i in range(19):
            _insert(conn, f"p{i}", "用户", "偏好", f"第{i}条偏好")
        report = build_report(conn, "u")
        assert report["counts"]["conflicts"] == 0
        assert report["conflicts"] == []
    finally:
        conn.close()


def test_a_predicate_that_is_multi_valued_only_by_type_is_not_a_conflict():
    """`is_multi_valued('知识', 'semantic')` is False but `(..., 'task')` is True.

    Omitting memory_type reintroduces the bug for every task-type fact, which is
    the largest false-positive class on the live store (待办 / 待补字段).
    """
    conn = connect_for_tests(MemConfig())
    try:
        _insert(conn, "t1", "项目", "待补字段", "负责人", memory_type="task")
        _insert(conn, "t2", "项目", "待补字段", "预算", memory_type="task")
        assert build_report(conn, "u")["counts"]["conflicts"] == 0
    finally:
        conn.close()


def test_a_genuine_single_valued_conflict_is_still_reported():
    """The detector must not become useless in the other direction: a real
    contradiction under a single-valued attribute is exactly what it exists for."""
    conn = connect_for_tests(MemConfig())
    try:
        _insert(conn, "c1", "我的职业", "是", "工程师", created_at=1000)
        _insert(conn, "c2", "我的职业", "是", "架构师", created_at=2000)
        report = build_report(conn, "u")
        assert report["counts"]["conflicts"] == 1
        entry = report["conflicts"][0]
        assert entry["kind"] == "single_valued_conflict"
        assert sorted(entry["fact_ids"]) == ["c1", "c2"]
        # The newer assertion is identified, since that is what the write path
        # would have kept — the reader needs it to judge.
        assert entry["detail"]["newest_fact_id"] == "c2"
    finally:
        conn.close()


def test_duplicates_are_found_by_fingerprint():
    conn = connect_for_tests(MemConfig())
    try:
        _insert(conn, "f1", "a", "p", "x", fingerprint="fp-same")
        _insert(conn, "f2", "a", "p", "x", fingerprint="fp-same")
        _insert(conn, "f3", "b", "q", "y", fingerprint="fp-other")
        report = build_report(conn, "u")
        assert report["counts"]["duplicates"] == 1
        assert sorted(report["duplicates"][0]["fact_ids"]) == ["f1", "f2"]
    finally:
        conn.close()


def test_a_clean_store_reports_nothing_and_does_not_raise():
    conn = connect_for_tests(MemConfig())
    try:
        report = build_report(conn, "u")
        assert report["counts"] == {"duplicates": 0, "conflicts": 0}
        assert report["duplicates"] == [] and report["conflicts"] == []
    finally:
        conn.close()


def test_non_active_rows_are_ignored():
    conn = connect_for_tests(MemConfig())
    try:
        _insert(conn, "f1", "a", "p", "x", fingerprint="fp-same")
        _insert(conn, "f2", "a", "p", "x", fingerprint="fp-same")
        conn.execute("UPDATE facts SET status = 'archived' WHERE fact_id = 'f2'")
        conn.commit()
        assert build_report(conn, "u")["counts"]["duplicates"] == 0
    finally:
        conn.close()