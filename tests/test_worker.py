"""Focused tests for the Worker queue lifecycle: orphaned `running` reclaim,
plus the changelog event a successful write must emit."""

from __future__ import annotations

import asyncio
import json
import time
import uuid

import pytest

from atom_memory.config import MemConfig
from atom_memory.models import (
    TYPE_DECISION_RULE,
    TYPE_SEMANTIC,
    FactCandidate,
)
from atom_memory.worker import TASK_DEAD, TASK_PENDING, TASK_RUNNING, Worker
from atom_memory.db import connect_for_tests


def _task_id() -> str:
    return str(uuid.uuid4())


def _enqueue(conn, status: str, payload: dict | None = None) -> str:
    tid = _task_id()
    conn.execute(
        "INSERT INTO task_queue(task_id, task_type, payload, status, priority, "
        "retry_count, max_retries, created_at) VALUES (?, ?, ?, ?, 5, 0, 3, ?)",
        (tid, "extract", json.dumps(payload or {"dummy": True}), status, 1),
    )
    conn.commit()
    return tid


def test_start_reclaims_orphaned_running_tasks():
    """A `running` task left by a crashed/cancelled worker is re-enqueued on
    the next `start()`, so the unit of work is never permanently lost."""

    async def scenario():
        conn = connect_for_tests()
        try:
            tid = _enqueue(conn, TASK_RUNNING)

            async def embed(_text: str) -> bytes:
                return b"\x00"

            worker = Worker(conn=conn, embed_func=embed, poll_interval_sec=0.01)
            # start() is synchronous and only *schedules* the drain loop (which
            # cannot run until the test awaits); the reclaim is observable now.
            worker.start()

            row = conn.execute(
                "SELECT status FROM task_queue WHERE task_id = ?", (tid,)
            ).fetchone()
            assert row["status"] == TASK_PENDING, (
                "a pre-existing `running` row must be reclaimed to `pending` on start"
            )

            await worker.stop()
        finally:
            conn.close()

    asyncio.run(scenario())


def test_stop_requeues_inflight_running_task():
    """A `running` task interrupted by `stop()` mid-task is returned to
    `pending`, so the unit of work is re-consumed by the next start (not only
    reclaimed on a *restart*, which would strand it until then)."""
    async def scenario():
        conn = connect_for_tests()
        try:
            tid = _task_id()
            conn.execute(
                "INSERT INTO task_queue(task_id, task_type, payload, status, "
                "priority, retry_count, max_retries, created_at) "
                "VALUES (?, 'persist_pre', ?, 'pending', 5, 0, 3, 1)",
                (tid, json.dumps({
                    "candidate_id": "c_batch", "user_id": "u1",
                    "session_id": "s1", "turn_id": 0,
                    "candidates": [
                        {"subject": "用户", "predicate": "偏好", "object": "黑咖啡"},
                    ],
                })),
            )
            conn.commit()

            entered = asyncio.Event()

            def embed(_text: str) -> bytes:
                # Cancel can only land while the worker awaits; keep the thread
                # busy so the persist does not finish before `stop()`.
                entered.set()
                time.sleep(1.0)
                return b"\x00"

            worker = Worker(conn=conn, embed_func=embed, poll_interval_sec=0.01)
            worker.start()
            await entered.wait()
            claimed = conn.execute(
                "SELECT status FROM task_queue WHERE task_id = ?", (tid,)
            ).fetchone()
            assert claimed["status"] == TASK_RUNNING

            await worker.stop()  # cancels mid-embed

            row = conn.execute(
                "SELECT status FROM task_queue WHERE task_id = ?", (tid,)
            ).fetchone()
            assert row["status"] == TASK_PENDING, (
                "an interrupted mid-task row must be requeued to pending"
            )
        finally:
            conn.close()

    asyncio.run(scenario())


def test_no_running_rows_is_a_noop():
    """Reclaim touches nothing when there are no orphaned rows."""

    async def scenario():
        conn = connect_for_tests()
        try:
            tid = _enqueue(conn, TASK_PENDING)

            async def embed(_text: str) -> bytes:
                return b"\x00"

            worker = Worker(conn=conn, embed_func=embed, poll_interval_sec=0.01)
            worker.start()
            row = conn.execute(
                "SELECT status FROM task_queue WHERE task_id = ?", (tid,)
            ).fetchone()
            assert row["status"] == TASK_PENDING
            await worker.stop()
        finally:
            conn.close()

    asyncio.run(scenario())


# ---- the changelog event -----------------------------------------------------


def _write_one(conn, memory_type: str, predicate: str = "决定") -> list:
    """Persist one candidate through the real write gate; return its events.

    The embed stub returns a correctly-shaped vector: the write path indexes the
    fact into ``facts_vec``, whose ``vec0`` column is a 512-dim float32 blob, and
    a short stub makes the insert fail rather than the assertion under test.
    """
    worker = Worker(
        conn=conn,
        embed_func=lambda _text: b"\x00" * (512 * 4),
        poll_interval_sec=0.01,
        config=MemConfig(),
    )
    candidate = FactCandidate(
        candidate_id=str(uuid.uuid4()),
        user_id="u1",
        session_id="s1",
        subject="项目",
        predicate=predicate,
        object="先回滚再排查",
        type=memory_type,
    )
    outcome = asyncio.run(worker._apply_candidates([candidate], "u1", "s1"))
    assert outcome["written"], "fixture assumption: the write must land"
    return conn.execute(
        "SELECT type, payload FROM events WHERE user_id = 'u1' ORDER BY created_at"
    ).fetchall()


def test_a_successful_write_is_recorded_in_the_changelog():
    """The hole this closed: a *written* fact used to leave no event at all.

    Refusals and reorganisations were logged while the ordinary path was silent,
    so a store that only ever grew looked unchanged — and the work overview's
    refresh trigger reads exactly this log.
    """
    conn = connect_for_tests()
    try:
        rows = _write_one(conn, TYPE_DECISION_RULE)
        written = [r for r in rows if r["type"] == "fact_written"]
        assert len(written) == 1, [r["type"] for r in rows]
    finally:
        conn.close()


def test_the_written_event_records_the_type_and_scope():
    """The refresh gate classifies on ``type``; the scope places the change."""
    conn = connect_for_tests()
    try:
        rows = _write_one(conn, TYPE_DECISION_RULE)
        payload = json.loads(
            next(r for r in rows if r["type"] == "fact_written")["payload"]
        )
        assert payload["type"] == TYPE_DECISION_RULE
        assert payload["subject"] == "项目"
        assert "scope_id" in payload
        assert payload["fact_id"]
    finally:
        conn.close()


def test_a_detail_write_is_recorded_as_its_own_type():
    """An attribute must arrive as ``semantic`` so it classifies as a detail."""
    conn = connect_for_tests()
    try:
        rows = _write_one(conn, TYPE_SEMANTIC, predicate="属性")
        payload = json.loads(
            next(r for r in rows if r["type"] == "fact_written")["payload"]
        )
        assert payload["type"] == TYPE_SEMANTIC
    finally:
        conn.close()


# ---- the failure path --------------------------------------------------------


def test_a_failed_task_closes_its_candidate_in_the_same_transaction():
    """The candidate's terminal state and the task's must not be split.

    They used to be two commits. A crash between them left a candidate marked
    ``error`` against a task still ``running`` — and since the retry re-runs the
    extraction while `_finish_candidate` is terminal-only, the candidate row
    could never be transitioned again. Whatever the outcome, the two rows must
    agree.
    """
    conn = connect_for_tests()
    try:
        worker = Worker(
            conn=conn,
            embed_func=lambda _text: b"\x00" * (512 * 4),
            poll_interval_sec=0.01,
            max_retries=1,
            config=MemConfig(),
        )
        # A task whose handler cannot succeed: the extractor raises, which is the
        # realistic failure this path exists for.
        tid = _task_id()
        conn.execute(
            "INSERT INTO fact_candidates(candidate_id, user_id, session_id, "
            "turn_id, status, created_at) VALUES ('c1', 'u1', 's1', 1, 'pending', 1)"
        )
        conn.execute(
            "INSERT INTO task_queue(task_id, task_type, payload, status, priority, "
            "retry_count, max_retries, created_at) VALUES (?, 'extract', ?, 'running', "
            "5, 0, 1, 1)",
            (
                tid,
                json.dumps(
                    {
                        "candidate_id": "c1",
                        "user_id": "u1",
                        "session_id": "s1",
                        "turn_id": 1,
                        "raw_text": "whatever",
                    }
                ),
            ),
        )
        conn.commit()

        class _Exploding:
            def extract(self, *_args, **_kwargs):
                raise RuntimeError("extractor blew up")

        worker.extractor = _Exploding()
        row = conn.execute(
            "SELECT * FROM task_queue WHERE task_id = ?", (tid,)
        ).fetchone()
        asyncio.run(worker._handle_task(row))

        task = conn.execute(
            "SELECT status, error FROM task_queue WHERE task_id = ?", (tid,)
        ).fetchone()
        candidate = conn.execute(
            "SELECT status, reject_kind FROM fact_candidates WHERE candidate_id = 'c1'"
        ).fetchone()

        # max_retries=1 means the first failure is already terminal for the task.
        assert task["status"] == TASK_DEAD
        assert "extractor blew up" in task["error"]
        # And the candidate agrees, rather than sitting at `pending`/`running`.
        assert candidate["status"] == "error"
        assert candidate["reject_kind"] == "task_error"

        # The alarm is emitted only *after* the state it describes is durable.
        # `record_event` commits unconditionally, so emitting it while the
        # caller still held the transaction open would have been what persisted
        # both rows — putting a "task is dead" event in the log ahead of the row
        # saying so, and making the caller's own commit a no-op.
        events = conn.execute(
            "SELECT type FROM events WHERE type = 'task_dead'"
        ).fetchall()
        assert len(events) == 1
    finally:
        conn.close()


# ---- capacity archiving: its own transaction ---------------------------------


def _age(conn, fact_id: str, created_at: int) -> None:
    conn.execute("UPDATE facts SET created_at = ? WHERE fact_id = ?", (created_at, fact_id))
    conn.commit()


def test_capacity_archiving_commits_its_own_work_and_logs_it():
    """Archiving and its changelog entry must land together, atomically.

    The archiving UPDATEs run in an implicit transaction. Before this was its own
    unit, that transaction stayed open across the rest of the maintenance pass —
    and `record_event` commits unconditionally, so any later event would flush a
    half-finished archival to disk while `facts_archived` was never written. The
    archive happened and the audit trail did not.
    """
    conn = connect_for_tests()
    try:
        worker = Worker(
            conn=conn,
            embed_func=lambda _text: b"\x00" * (512 * 4),
            poll_interval_sec=0.01,
            config=MemConfig(max_active_facts=1, archive_protect_days=0),
        )
        # Two plain semantic facts, old enough to be archivable.
        for i in range(2):
            candidate = FactCandidate(
                candidate_id=str(uuid.uuid4()),
                user_id="u1",
                session_id="s1",
                subject=f"项目{i}",
                predicate="属性",
                object=f"值{i}",
                type=TYPE_SEMANTIC,
            )
            asyncio.run(worker._apply_candidates([candidate], "u1", "s1"))
        for row in conn.execute("SELECT fact_id FROM facts WHERE user_id = 'u1'").fetchall():
            _age(conn, row["fact_id"], 1)

        archived = worker.enforce_capacity("u1")
        assert len(archived) == 1, archived

        # The archive is committed, so it is visible without any further commit.
        active = conn.execute(
            "SELECT COUNT(*) AS n FROM facts WHERE user_id = 'u1' AND status = 'active'"
        ).fetchone()["n"]
        assert active == 1

        # And the audit entry is present, with the fact it archived.
        events = conn.execute(
            "SELECT payload FROM events WHERE type = 'facts_archived'"
        ).fetchall()
        assert len(events) == 1
        payload = json.loads(events[0]["payload"])
        assert payload["cap"] == 1
        assert [a["fact_id"] for a in payload["archived"]] == [archived[0]["fact_id"]]
    finally:
        conn.close()
