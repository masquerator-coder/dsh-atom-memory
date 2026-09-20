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
from atom_memory.worker import TASK_PENDING, TASK_RUNNING, Worker
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
