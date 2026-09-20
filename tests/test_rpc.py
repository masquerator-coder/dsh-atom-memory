"""Tests for the NDJSON stdio RPC service (rpc.py).

These drive a real ``python -m atom_memory.rpc`` child process the way the
dsh plugin does, so they assert the wire protocol end to end without any Node
dependency. matplotlib-free; uses only stdlib subprocess.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import time

import pytest

# The RPC host must not try to load a real embedding model in CI; the default
# library path would. We exercise non-embedding methods and the lifecycle here,
# plus add() which enqueues without embedding (the worker would embed, so we
# only assert the enqueue response, not persistence).
ADDR = [sys.executable, "-m", "atom_memory.rpc"]


@pytest.fixture
def proc(tmp_path):
    """Spawn an RPC child process pointing at a temp database."""
    env = {"PYTHONIOENCODING": "utf-8"}
    p = subprocess.Popen(
        ADDR,
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        encoding="utf-8",
        env={**os.environ, **env},
        cwd=str(tmp_path),
    )
    yield p
    # Tear down gracefully: send stop, wait briefly, then kill.
    try:
        p.stdin.write('{"id": "s", "method": "stop"}\n')
        p.stdin.flush()
    except (BrokenPipeError, OSError):
        pass
    try:
        p.wait(timeout=5)
    except subprocess.TimeoutExpired:
        p.kill()
        p.wait()


def _send(p, rid, method, params=None, flush=True):
    line = json.dumps({"id": rid, "method": method, "params": params or {}}, ensure_ascii=False)
    p.stdin.write(line + "\n")
    if flush:
        p.stdin.flush()


def _recv(p, timeout=15):
    """Read one NDJSON response line from stdout."""
    line = p.stdout.readline()
    deadline = time.time() + timeout
    while not line.strip() and time.time() < deadline:
        if p.poll() is not None:
            break
        line = p.stdout.readline()
    return json.loads(line) if line.strip() else None


def test_lifecycle_and_unknown_method(proc, tmp_path):
    p = proc
    db = str(tmp_path / "rpc.db")
    _send(p, 1, "start", {"db_path": db, "worker_poll_interval_sec": 0.05})
    resp = _recv(p)
    assert resp["id"] == 1 and resp["ok"] is True
    assert resp["result"]["started"] is True

    # health reports started
    _send(p, 2, "health")
    assert _recv(p)["result"]["started"] is True

    # unknown method -> ok false, error
    _send(p, 3, "nope")
    resp = _recv(p)
    assert resp["ok"] is False and "unknown method" in resp["error"]

    # stats works on an idle user
    _send(p, 4, "stats", {"user_id": "u1"})
    resp = _recv(p)
    assert resp["ok"] is True and resp["result"]["facts"] == 0


def test_add_enqueues_and_recall_after_start(proc, tmp_path):
    p = proc
    db = str(tmp_path / "rpc2.db")
    _send(p, 1, "start", {"db_path": db, "worker_poll_interval_sec": 0.05})
    assert _recv(p)["ok"] is True

    _send(p, 2, "add", {"user_id": "u1", "session_id": "s1", "text": "用户喜欢黑咖啡"})
    resp = _recv(p)
    assert resp["ok"] is True
    assert resp["result"]["status"] == "pending"
    assert "candidate_id" in resp["result"]

    # recall returns a structured result (facts may be empty while embedding
    # would run in the worker; we assert the shape, not the contents).
    _send(p, 3, "recall", {"user_id": "u1", "query": "咖啡"})
    resp = _recv(p)
    assert resp["ok"] is True
    for key in ("facts", "conflicts", "degraded", "token_count", "trace_id"):
        assert key in resp["result"]


def test_bad_arguments_reported(proc, tmp_path):
    p = proc
    _send(p, 1, "start", {"db_path": str(tmp_path / "bad.db")})
    assert _recv(p)["ok"] is True
    # call with a bogus kwarg for a method
    _send(p, 2, "stats", {"user_id": "u1", "bogus": 1})
    resp = _recv(p)
    assert resp["ok"] is False and "bad arguments" in resp["error"]


def test_method_before_start_errors(proc, tmp_path):
    p = proc
    # Without a start, an ordinary method must fail cleanly.
    _send(p, 1, "recall", {"user_id": "u1", "query": "x"})
    resp = _recv(p)
    assert resp["ok"] is False and "start" in resp["error"]


def test_invalid_json_line(proc, tmp_path):
    p = proc
    p.stdin.write("{not json\n")
    p.stdin.flush()
    resp = _recv(p)
    assert resp["ok"] is False and "invalid JSON" in resp["error"]


def test_persist_candidates_persists_type_and_content(proc, tmp_path):
    """LLM-style pre-extracted candidates persist with type + content via RPC."""
    p = proc
    db = str(tmp_path / "rpc3.db")
    _send(p, 1, "start", {"db_path": db, "worker_poll_interval_sec": 0.05})
    assert _recv(p)["ok"] is True

    _send(p, 2, "persist_candidates", {
        "user_id": "u1",
        "session_id": "s1",
        "turn_id": 1,
        "candidates": [
            {
                "subject": "用户",
                "predicate": "教训",
                "object": "先备份再升级",
                "type": "lesson",
                "content": "升级任何生产依赖前先做完整备份",
            },
            {
                "subject": "用户",
                "predicate": "偏好",
                "object": "黑咖啡",
                "type": "semantic",
            },
        ],
    })
    resp = _recv(p)
    assert resp["ok"] is True and resp["result"]["queued"] == 2
    assert "candidate_id" in resp["result"]

    # give the worker a moment to drain, then inspect the persisted facts
    import time as _time
    _time.sleep(1.0)
    _send(p, 3, "stats", {"user_id": "u1"})
    resp = _recv(p)
    assert resp["ok"] is True and resp["result"]["facts"] == 2

    # verify type/content survived in recall's fact dict
    _send(p, 4, "recall", {"user_id": "u1", "query": "升级"})
    resp = _recv(p)
    assert resp["ok"] is True
    lessons = [f for f in resp["result"]["facts"] if f.get("type") == "lesson"]
    assert len(lessons) == 1
    assert lessons[0]["content"] == "升级任何生产依赖前先做完整备份"



def test_lifecycle_and_write_receipts_over_the_wire(proc, tmp_path):
    """The new surface end to end: health, summary meta, supersede, purge.

    The write receipts are the point: the plugin's tools render what the store
    *decided* (applied / superseded / rejected), so the receipt has to survive
    the NDJSON hop intact — and `wait_ms` has to actually wait for the verdict
    instead of returning the enqueue receipt.
    """
    _send(proc, 1, "start", {"db_path": str(tmp_path / "rpc-lifecycle.db"),
                             "worker_poll_interval_sec": 0.05, "max_retries": 1})
    assert _recv(proc)["ok"] is True

    _send(proc, 2, "health", {})
    health = _recv(proc)["result"]
    assert health["started"] is True and health["ok"] is True
    assert "queue" in health and "index" in health and "db_path" in health

    # An empty store says so, in the same round trip that renders the digest:
    # this is what lets the injection path skip a snapshot entirely.
    _send(proc, 3, "summary", {
        "user_id": "u1", "max_tokens": 800, "detail": False, "include_meta": True,
    })
    meta = _recv(proc)["result"]
    assert meta["facts"] == 0 and isinstance(meta["text"], str)

    # Without include_meta the payload is still the plain string the tools and
    # the settings panel expect.
    _send(proc, 4, "summary", {"user_id": "u1", "max_tokens": 800, "detail": False})
    assert isinstance(_recv(proc)["result"], str)

    # A first write lands.
    _send(proc, 5, "add", {
        "user_id": "u1", "session_id": "s1", "text": "我的常用颜色是蓝色",
        "turn_id": 0, "wait_ms": 8000,
    })
    first = _recv(proc)["result"]
    assert first["status"] == "applied", first
    assert len(first["outcome"]["written"]) == 1
    fact_id = first["outcome"]["written"][0]

    # A newer assertion for the same single-valued attribute replaces it, and
    # the receipt names both values so the model can correct itself.
    _send(proc, 6, "add", {
        "user_id": "u1", "session_id": "s1", "text": "我的常用颜色是绿色",
        "turn_id": 0, "wait_ms": 8000,
    })
    second = _recv(proc)["result"]
    assert second["status"] == "applied", second
    assert len(second["outcome"]["superseded"]) == 1, second
    swapped = second["outcome"]["superseded"][0]
    assert swapped["old_object"] == "蓝色" and swapped["new_object"] == "绿色"

    _send(proc, 7, "recall", {"user_id": "u1", "query": "常用颜色"})
    facts = _recv(proc)["result"]["facts"]
    assert [f["object"] for f in facts] == ["绿色"]

    # Purge erases for real; the receipt distinguishes it from a soft forget.
    # Both the live value and the value it replaced are erased: a purge has to
    # take the row out of every index, not just out of the active set.
    active_id = second["outcome"]["written"][0]
    assert active_id != fact_id
    for offset, fid in enumerate((active_id, fact_id), start=1):
        _send(proc, 7 + offset, "forget", {
            "user_id": "u1", "fact_id": fid, "purge": True, "wait_ms": 8000,
        })
        assert _recv(proc)["result"]["outcome"]["purged"] == [fid]

    _send(proc, 20, "stats", {"user_id": "u1"})
    stats = _recv(proc)["result"]
    assert stats["facts"] == 0 and "archived" in stats and isinstance(stats["recent"], list)


# ---- the work-overview surface -----------------------------------------------
#
# The out-of-band job's whole vocabulary: ask whether a refresh is warranted,
# fetch the deterministic material to write from, store the prose, and read the
# changelog. These drive it over the real wire because the dsh half depends on
# the exact shapes.


def _start(proc, tmp_path, name="ov.db"):
    _send(proc, 1, "start", {
        "db_path": str(tmp_path / name),
        "worker_poll_interval_sec": 0.05,
        "max_retries": 1,
    })
    assert _recv(proc)["ok"] is True


def _seed_one_rule(proc, rid=2, next_rid=3):
    """Write one decision rule through the real pipeline; returns the next rid.

    Uses ``persist_candidates`` rather than ``add``: the latter runs the Python
    rule engine, whose patterns are narrow, so a fixture phrased like a real
    fact ("决定：…") is reported ``skipped`` rather than stored. The dsh plugin
    takes this path in production — it extracts with the LLM and ships typed
    candidates — so this is also the more faithful stand-in.
    """
    _send(proc, rid, "persist_candidates", {
        "user_id": "u1", "session_id": "s1", "turn_id": 0, "wait_ms": 8000,
        "candidates": [{
            "subject": "dsh-atom-memory", "predicate": "决定",
            "object": "摘要先给工作总览", "type": "decision_rule",
            "importance": 0.9, "confidence": 0.9,
        }],
    })
    resp = _recv(proc)
    assert resp["ok"] is True, resp
    assert resp["result"]["status"] == "applied", resp["result"]
    return next_rid


def test_overview_status_on_an_empty_store(proc, tmp_path):
    p = proc
    _start(p, tmp_path)
    _send(p, 2, "overview_status", {"user_id": "u1"})
    status = _recv(p)["result"]
    assert status["cached"] is False
    assert status["stale"] is True
    # Nothing to narrate, so no refresh is warranted however stale it looks.
    assert status["should_refresh"] is False
    assert status["refresh_reason"] == "no_facts"


def test_overview_skeleton_is_returned_over_the_wire(proc, tmp_path):
    p = proc
    _start(p, tmp_path)
    next_rid = _seed_one_rule(p)

    _send(p, next_rid, "overview_skeleton", {"user_id": "u1"})
    resp = _recv(p)
    assert resp["ok"] is True, resp
    skeleton = resp["result"]
    assert skeleton["totals"]["facts"] == 1
    assert len(skeleton["units"]) == 1
    assert skeleton["fingerprint"]
    # It has to survive JSON: the aggregation holds per-fact rows while grouping
    # and must not leak them into the payload.
    assert "_candidates" not in json.dumps(skeleton, ensure_ascii=False)


def test_overview_put_then_read_back_through_summary(proc, tmp_path):
    p = proc
    _start(p, tmp_path)
    next_rid = _seed_one_rule(p)

    _send(p, next_rid, "overview_status", {"user_id": "u1"})
    status = _recv(p)["result"]
    assert status["should_refresh"] is True
    assert status["refresh_reason"] == "not_cached"

    prose = "- 完成了记忆摘要改造：摘要先给工作总览，再给查询指路。"
    _send(p, next_rid + 1, "overview_put", {
        "user_id": "u1", "text": prose, "facts_count": 1,
    })
    assert _recv(p)["result"]["stored"] is True

    # The freeze path asks for the head and gets the stored prose verbatim.
    _send(p, next_rid + 2, "summary", {
        "user_id": "u1", "max_tokens": 600, "detail": False,
        "overview": True, "include_meta": True,
    })
    meta = _recv(p)["result"]
    assert meta["overview"]["cached"] is True
    assert prose in meta["text"]
    assert "以前做过的工作" in meta["text"]
    assert "要了解细节" in meta["text"]


def test_overview_put_refuses_empty_text(proc, tmp_path):
    """An empty generation must not overwrite a good overview."""
    p = proc
    _start(p, tmp_path)
    next_rid = _seed_one_rule(p)
    _send(p, next_rid, "overview_put", {"user_id": "u1", "text": "   "})
    resp = _recv(p)
    assert resp["ok"] is False and "non-empty" in resp["error"]


def test_summary_without_the_overview_flag_is_unchanged(proc, tmp_path):
    """Compatibility: an older dsh keeps its exact previous output."""
    p = proc
    _start(p, tmp_path)
    next_rid = _seed_one_rule(p)
    _send(p, next_rid, "overview_put", {"user_id": "u1", "text": "总览正文"})

    _send(p, next_rid + 1, "summary", {
        "user_id": "u1", "max_tokens": 600, "detail": False,
    })
    text = _recv(p)["result"]
    assert "以前做过的工作" not in text
    assert "要了解细节" not in text


def test_the_head_degrades_when_nothing_is_cached(proc, tmp_path):
    """No cache is not an error: the deterministic head is rendered instead."""
    p = proc
    _start(p, tmp_path)
    next_rid = _seed_one_rule(p)
    _send(p, next_rid, "summary", {
        "user_id": "u1", "max_tokens": 400, "detail": False,
        "overview": True, "include_meta": True,
    })
    meta = _recv(p)["result"]
    assert meta["overview"]["cached"] is False
    assert "以前做过的工作" in meta["text"]
    assert "要了解细节" in meta["text"]


def test_changes_lists_what_the_store_did(proc, tmp_path):
    p = proc
    _start(p, tmp_path)
    next_rid = _seed_one_rule(p)

    _send(p, next_rid, "changes", {"user_id": "u1"})
    result = _recv(p)["result"]
    assert isinstance(result["changes"], list)
    written = [c for c in result["changes"] if c["type"] == "fact_written"]
    assert len(written) == 1
    assert written[0]["detail"]["type"] == "decision_rule"
    assert result["level"] == "structural"


def test_overview_methods_require_start(proc, tmp_path):
    """Every new method fails cleanly before the store is started."""
    p = proc
    for rid, method in enumerate(
        ("overview_status", "overview_skeleton", "changes"), start=1
    ):
        _send(p, rid, method, {"user_id": "u1"})
        resp = _recv(p)
        assert resp["ok"] is False and "start" in resp["error"], method
