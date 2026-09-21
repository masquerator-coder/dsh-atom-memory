"""Hardening round: content identity, truncation visibility, leases, per-fact caps.

One file for the findings that share a theme — *the store must not quietly change
what it was given, and two consumers must not quietly do the same work twice*:

- **F19** content identity: the same claim seen twice reinforces the stored fact
  instead of adding a near-duplicate row, including for knowledge bodies and
  multi-valued facts, which had no dedup at all before. The fingerprint carries
  claim polarity, so a correction is never mistaken for a repeat.
- **F21** truncation is reported: the ingest caps still apply, but a shortened
  write now says so in the receipt instead of silently answering questions about
  text the store never kept.
- **F22** the recall budget is bounded per fact: one long body can no longer
  overshoot the budget it claims to respect, and `get_fact` returns the rest.
- **F03** claims are attributable and time-bounded: a live worker's task is not
  stolen by a second worker starting up, while an expired lease is reclaimed.
- **F25** the token estimate counts Latin text at 4 characters per token.
- **F-2.3** the reuse-and-decay curve is configuration, not a constant.
- **F07/F17** the dead `pending` channel is gone, and `llm_extractor` is refused
  over the wire instead of being silently dropped next to a comment that
  described an implementation nobody wrote.
"""

from __future__ import annotations

import asyncio
import json
import time

import pytest

from atom_memory.api import AtomMem
from atom_memory.config import MemConfig
from atom_memory.db import connect_for_tests
from atom_memory.embedder import serialize_float32
from atom_memory.fingerprint import content_fingerprint
from atom_memory.models import FactCandidate
from atom_memory.reinforce import DEFAULT_CURVE, ReinforceCurve, adjust
from atom_memory.retriever import CHARS_PER_TOKEN, estimate_tokens
from atom_memory.sanitize import clean_body_meta, clean_field_meta
from atom_memory.worker import Worker

DIM = 512


class _MarkerEmbedder:
    """Deterministic embedder: text with the marker is near text with the marker.

    The store's near-duplicate gate compares embeddings, so the test needs an
    embedder whose behaviour is legible: everything containing ``MARKER`` maps to
    one vector, everything else to an orthogonal one. That makes "reworded copy"
    and "different document" two unambiguous cases instead of a similarity
    argument.
    """

    MARKER = "备份"

    def embed_one(self, text: str) -> bytes:
        if self.MARKER in text:
            return serialize_float32([1.0] * DIM)
        return serialize_float32([0.0, 1.0] * (DIM // 2))


def _make(tmp_path, monkeypatch, embedder=_MarkerEmbedder, **overrides) -> AtomMem:
    monkeypatch.setattr("atom_memory.api.Embedder", lambda **_: embedder())
    kwargs = dict(
        db_path=str(tmp_path / "mem.db"),
        worker_poll_interval_sec=0.02,
        max_retries=1,
        write_ack_timeout_ms=5000,
    )
    kwargs.update(overrides)
    return AtomMem(MemConfig(**kwargs))


async def _candidates(mem, items) -> dict:
    """Run candidates straight through the worker's apply path."""
    return await mem._worker._apply_candidates(list(items), "u1", "s1")


def _knowledge(
    content: str,
    *,
    subject: str = "用户",
    predicate: str = "知识",
    object_hint: str = "",
) -> FactCandidate:
    """Build a knowledge candidate whose title is derived, not dictated.

    ``object_hint`` lets a test vary the derived title while keeping the body
    identical — the shape a second capture of the same procedure actually has.
    """
    title = object_hint or content[:40]
    return FactCandidate(
        candidate_id=f"c-{abs(hash(content + title)) % 10**8}",
        user_id="u1",
        session_id="s1",
        subject=subject,
        predicate=predicate,
        object=title,
        content=content,
        type="sop",
        confidence=0.8,
        importance=0.7,
    )


# -- F19: content identity ----------------------------------------------------

def test_the_same_body_written_twice_is_folded_into_one_memory(tmp_path, monkeypatch):
    """A restated body reinforces its fact instead of adding a second row.

    The second capture carries a *different title* on purpose: for knowledge the
    title is derived from the body (the summary titles them that way), so it is
    not part of the identity. This is the duplicate the store used to keep.
    """
    async def scenario() -> None:
        mem = _make(tmp_path, monkeypatch)
        await mem.start()
        try:
            body = "升级任何生产依赖前先做完整备份，并保留回滚包。" * 20
            first = await _candidates(mem, [_knowledge(body, object_hint="旧标题")])
            assert len(first["written"]) == 1

            second = await _candidates(mem, [_knowledge(body, object_hint="新标题")])
            assert second["written"] == [], second
            assert len(second["reinforced"]) == 1
            # Recognised by content identity, without an embedding comparison.
            assert second["reinforced"][0]["on"] == "fingerprint"
            assert mem.list_facts("u1")["total"] == 1

            events = mem.db.execute(
                "SELECT COUNT(*) AS n FROM events WHERE type = 'fact_deduplicated'"
            ).fetchone()
            assert events["n"] >= 1
        finally:
            await mem.stop()

    asyncio.run(scenario())


def test_the_stored_row_records_its_content_identity(tmp_path, monkeypatch):
    """The fingerprint is persisted, so identity is auditable after the fact."""
    async def scenario() -> None:
        mem = _make(tmp_path, monkeypatch)
        await mem.start()
        try:
            await _candidates(mem, [_knowledge("一段知识正文。" * 30)])
            row = mem.db.execute(
                "SELECT content_fingerprint FROM facts WHERE status = 'active'"
            ).fetchone()
            assert row["content_fingerprint"] and len(row["content_fingerprint"]) == 32
        finally:
            await mem.stop()

    asyncio.run(scenario())


def test_a_reworded_body_within_the_gate_is_also_folded(tmp_path, monkeypatch):
    """The semantic gate catches a reformatted-but-same document."""
    async def scenario() -> None:
        mem = _make(tmp_path, monkeypatch, dedup_min_body_chars=50)
        await mem.start()
        try:
            original = "部署前必须完成备份。" * 30
            await _candidates(mem, [_knowledge(original)])
            # Same topic (same marker → same embedding), different wording, same
            # type/subject/predicate, long enough to be a document.
            reworded = "发布之前一定要先备份完整数据。" * 30
            outcome = await _candidates(mem, [_knowledge(reworded)])
            assert outcome["written"] == [], outcome
            assert outcome["reinforced"][0]["on"] == "embedding"
            assert mem.list_facts("u1")["total"] == 1
        finally:
            await mem.stop()

    asyncio.run(scenario())


def test_a_different_document_is_still_a_different_memory(tmp_path, monkeypatch):
    """The gate must not merge two genuinely distinct bodies."""
    async def scenario() -> None:
        mem = _make(tmp_path, monkeypatch, dedup_min_body_chars=50)
        await mem.start()
        try:
            await _candidates(mem, [_knowledge("备份流程的第一步。" * 30)])
            # No marker → orthogonal vector → outside the gate.
            outcome = await _candidates(mem, [_knowledge("颜色偏好与部署无关。" * 30)])
            assert len(outcome["written"]) == 1
            assert mem.list_facts("u1")["total"] == 2
        finally:
            await mem.stop()

    asyncio.run(scenario())


def test_a_disabled_gate_keeps_only_the_exact_test(tmp_path, monkeypatch):
    """`dedup_max_distance = 0` turns the semantic half off, not the fingerprint."""
    async def scenario() -> None:
        mem = _make(tmp_path, monkeypatch, dedup_max_distance=0, dedup_min_body_chars=50)
        await mem.start()
        try:
            body = "备份流程的第一步。" * 30
            await _candidates(mem, [_knowledge(body)])
            reworded = await _candidates(mem, [_knowledge("开始备份之前。" * 30)])
            assert len(reworded["written"]) == 1          # not merged
            again = await _candidates(mem, [_knowledge(body)])
            assert again["written"] == []                 # exact repeat still is
        finally:
            await mem.stop()

    asyncio.run(scenario())


def test_polarity_is_part_of_the_identity():
    """A negation is a different claim, not a repeat of the positive one."""
    positive = content_fingerprint(
        user_id="u1", type="semantic", subject="用户", predicate="偏好", object="咖啡"
    )
    negative = content_fingerprint(
        user_id="u1", type="semantic", subject="用户", predicate="偏好", object="咖啡",
        negated=True,
    )
    assert positive != negative


def test_an_owner_blind_key_would_collide_so_the_owner_is_in_it():
    """The candidate table's key is globally UNIQUE, so identity includes the owner."""
    a = content_fingerprint(
        user_id="alice", type="semantic", subject="用户", predicate="职业", object="工程师"
    )
    b = content_fingerprint(
        user_id="bob", type="semantic", subject="用户", predicate="职业", object="工程师"
    )
    assert a != b


# -- F21: truncation is reported ---------------------------------------------

def test_a_capped_write_says_it_was_capped(tmp_path, monkeypatch):
    async def scenario() -> None:
        mem = _make(tmp_path, monkeypatch, max_content_chars=500, write_ack_timeout_ms=5000)
        await mem.start()
        try:
            receipt = await mem.add("u1", "s1", "长" * 4000)
            truncated = receipt["outcome"]["truncated"]
            assert truncated[0]["field"] == "content"
            assert truncated[0]["original_chars"] == 4000
            assert truncated[0]["kept_chars"] == 500
        finally:
            await mem.stop()

    asyncio.run(scenario())


def test_a_shortened_write_is_reported_once_not_twice(tmp_path, monkeypatch):
    """The same loss must not be counted twice in the receipt.

    A loss reaches the receipt from two sides: the calling write shortened the
    text before enqueueing, and the worker shortened the extracted fields and
    stored its own records on the candidate. When both cover the same field the
    naive concatenation reported one shortened field twice, so a model reading
    the receipt saw double the actual loss.
    """
    async def scenario() -> None:
        mem = _make(tmp_path, monkeypatch, max_content_chars=500, write_ack_timeout_ms=5000)
        await mem.start()
        try:
            receipt = await mem.add("u1", "s1", "长" * 4000)
            truncated = receipt["outcome"]["truncated"]
            content_records = [r for r in truncated if r.get("field") == "content"]
            assert len(content_records) == 1, f"reported {len(content_records)}x: {truncated}"

            # The stored outcome is the other side of the merge, and it must
            # agree with the receipt rather than carry an extra copy.
            row = mem.db.execute(
                "SELECT result_fact_ids FROM fact_candidates WHERE candidate_id = ?",
                (receipt["candidate_id"],),
            ).fetchone()
            import json as _json

            stored = _json.loads(row["result_fact_ids"] or "{}")
            stored_content = [
                r for r in (stored.get("truncated") or []) if r.get("field") == "content"
            ]
            assert len(stored_content) <= 1, stored
        finally:
            await mem.stop()

    asyncio.run(scenario())


def test_distinct_truncations_all_survive_the_merge(tmp_path, monkeypatch):
    """De-duplication must not swallow a genuinely different loss."""
    from atom_memory.api import _merge_truncation_records

    content = {"field": "content", "original_chars": 400, "kept_chars": 30}
    obj = {"field": "object", "original_chars": 150, "kept_chars": 20}
    # Identical records collapse; different fields both stay.
    assert _merge_truncation_records([content], [content]) == [content]
    assert _merge_truncation_records([content], [obj]) == [content, obj]
    # The same field shortened to a *different* length is a different event.
    other_kept = {"field": "content", "original_chars": 400, "kept_chars": 40}
    assert _merge_truncation_records([content], [other_kept]) == [content, other_kept]
    # Missing / empty sides pass the other through unchanged.
    assert _merge_truncation_records(None, [content]) == [content]
    assert _merge_truncation_records([content], None) == [content]


def test_an_untouched_write_reports_no_truncation(tmp_path, monkeypatch):
    async def scenario() -> None:
        mem = _make(tmp_path, monkeypatch, max_content_chars=500)
        await mem.start()
        try:
            receipt = await mem.add("u1", "s1", "我的常用颜色是蓝色")
            # The outcome shape is stable: `truncated` is always present, empty
            # when nothing was shortened (a conditional key would make callers
            # guess whether "no key" means "nothing lost" or "old store").
            assert (receipt.get("outcome") or {}).get("truncated") == []
        finally:
            await mem.stop()

    asyncio.run(scenario())


def test_the_sanitizer_reports_the_loss_it_causes():
    cleaned = clean_body_meta("词" * 50, 20)
    assert cleaned.truncated
    assert cleaned.original_chars == 50 and cleaned.kept_chars == 20
    assert cleaned.record("content") == {
        "field": "content", "original_chars": 50, "kept_chars": 20,
    }
    whole = clean_field_meta("短", 20)
    assert whole.truncated is False and whole.record("object") == {}


def test_the_panel_write_paths_report_truncation_too(tmp_path, monkeypatch):
    async def scenario() -> None:
        mem = _make(tmp_path, monkeypatch, max_field_chars=10)
        await mem.start()
        try:
            receipt = await mem.add("u1", "s1", "我的常用颜色是蓝色")
            fact_id = (receipt.get("outcome") or {}).get("written", [None])[0]
            assert fact_id
            edited = await mem.edit_fact("u1", fact_id, object="非" * 40)
            assert edited["truncated"][0]["field"] == "object"
            assert edited["truncated"][0]["original_chars"] == 40
        finally:
            await mem.stop()

    asyncio.run(scenario())


# -- F22: per-fact ceiling ----------------------------------------------------

def test_one_long_fact_cannot_overshoot_the_recall_budget(tmp_path, monkeypatch):
    async def scenario() -> None:
        mem = _make(tmp_path, monkeypatch, max_fact_tokens=120, max_retries=1)
        await mem.start()
        try:
            body = "备份流程很长的一段正文。" * 400          # thousands of tokens
            await _candidates(mem, [_knowledge(body)])
            result = await mem.recall("u1", "备份", token_budget=200, top_k=5)
            assert result["facts"], "the first match is still returned"
            fact = result["facts"][0]
            assert fact["truncated"] is True
            # Bounded by the ceiling (plus the SPO line), not by the body length.
            assert result["token_count"] <= 200

            full = mem.get_fact("u1", fact["fact_id"])
            assert full["content"] == body
        finally:
            await mem.stop()

    asyncio.run(scenario())


def test_a_fact_under_the_ceiling_is_not_touched(tmp_path, monkeypatch):
    async def scenario() -> None:
        mem = _make(tmp_path, monkeypatch, max_fact_tokens=1000)
        await mem.start()
        try:
            await _candidates(mem, [_knowledge("短正文。" * 10)])
            result = await mem.recall("u1", "短正文", token_budget=2000, top_k=5)
            assert result["facts"][0]["truncated"] is False
        finally:
            await mem.stop()

    asyncio.run(scenario())


def test_get_fact_refuses_another_users_fact(tmp_path, monkeypatch):
    async def scenario() -> None:
        mem = _make(tmp_path, monkeypatch)
        await mem.start()
        try:
            await _candidates(mem, [_knowledge("正文。" * 30)])
            fact_id = mem.list_facts("u1")["facts"][0]["fact_id"]
            with pytest.raises(ValueError):
                mem.get_fact("someone-else", fact_id)
        finally:
            await mem.stop()

    asyncio.run(scenario())


# -- F03: claim attribution and leases ----------------------------------------

def test_a_live_claim_is_not_stolen_and_an_expired_lease_is(tmp_path):
    """Reclaim is about *whose lease expired*, not *which rows look abandoned*."""
    conn = connect_for_tests()
    now = int(time.time() * 1000)
    rows = [
        ("live", "pending", "other-worker", now + 60_000),      # lease still valid
        ("stale", "pending", "dead-worker", now - 60_000),      # lease expired
    ]
    for task_id, _status, owner, lease in rows:
        conn.execute(
            "INSERT INTO task_queue(task_id, task_type, payload, status, priority, "
            "retry_count, max_retries, created_at, started_at, claimed_by, lease_expires_at) "
            "VALUES (?, 'extract', '{}', 'running', 0, 0, 3, ?, ?, ?, ?)",
            (task_id, now, now, owner, lease),
        )
    conn.commit()

    async def scenario() -> None:
        worker = Worker(conn, lambda text: serialize_float32([1.0] * DIM))
        worker.start()
        try:
            statuses = {
                r["task_id"]: (r["status"], r["claimed_by"])
                for r in conn.execute("SELECT task_id, status, claimed_by FROM task_queue")
            }
            # The peer's task keeps running under its owner — this worker did not
            # touch it, even though it was `running` when this worker started.
            assert statuses["live"][0] == "running"
            assert statuses["live"][1] == "other-worker"
            # The expired lease is reclaimed: the dead owner's claim is gone, so
            # the row is either back in the queue or already re-claimed — but
            # never still attributed to the worker that stopped existing.
            assert statuses["stale"][1] != "dead-worker"
        finally:
            await worker.stop()

    asyncio.run(scenario())
    conn.close()


def test_a_claim_is_attributable_and_released_when_the_task_ends(tmp_path, monkeypatch):
    async def scenario() -> None:
        mem = _make(tmp_path, monkeypatch)
        await mem.start()
        try:
            await mem.add("u1", "s1", "我的常用颜色是蓝色")
            deadline = time.monotonic() + 10
            while time.monotonic() < deadline:
                row = mem.db.execute(
                    "SELECT status, claimed_by, lease_expires_at FROM task_queue "
                    "ORDER BY created_at DESC LIMIT 1"
                ).fetchone()
                if row["status"] in ("done", "dead", "failed"):
                    break
                await asyncio.sleep(0.05)
            assert row["status"] == "done", dict(row)
            # Finished work holds no claim.
            assert row["claimed_by"] is None
            assert row["lease_expires_at"] is None
            assert mem._worker.worker_id  # the identity written while it ran
        finally:
            await mem.stop()

    asyncio.run(scenario())


# -- F07 / F17 -----------------------------------------------------------------

def test_recall_no_longer_advertises_a_pending_channel(tmp_path, monkeypatch):
    """The channel nothing wrote and nothing could approve is gone."""
    async def scenario() -> None:
        mem = _make(tmp_path, monkeypatch)
        await mem.start()
        try:
            assert not hasattr(mem, "_load_pending")
            result = await mem.recall("u1", "任何查询")
            assert "pending" not in result
            assert set(result) >= {"facts", "conflicts", "degraded", "token_count", "trace_id"}
        finally:
            await mem.stop()

    asyncio.run(scenario())


def test_llm_extractor_is_refused_over_the_wire():
    """A callable cannot cross the wire; saying so beats silently dropping it."""
    from atom_memory.rpc import RpcServer, _RpcError

    server = RpcServer()
    with pytest.raises(_RpcError) as excinfo:
        asyncio.run(server._start({"llm_extractor": object()}))
    assert "persist_candidates" in str(excinfo.value)


# -- F25: the token estimate ---------------------------------------------------

def test_latin_text_is_counted_at_four_characters_per_token():
    assert CHARS_PER_TOKEN == 4
    # 12 non-CJK characters → 3 tokens, where the old divisor said 2.
    assert estimate_tokens("hello world!") == 3
    # CJK is unaffected: one character is one token.
    assert estimate_tokens("你好世界") == 4
    assert estimate_tokens("") == 0


# -- F-2.3: the reuse-and-decay curve is configuration -------------------------

def test_the_curve_is_built_from_configuration():
    config = MemConfig(
        reinforce_a_max=0.9,
        reinforce_n_half=1.0,
        reinforce_half_life_days=7.0,
        reinforce_cooldown_sec=60.0,
    )
    curve = ReinforceCurve.from_config(config)
    assert curve.a_max == 0.9
    assert curve.n_half == 1.0
    assert curve.half_life_days == 7.0
    assert curve.cooldown_sec == 60.0
    # Derived rates follow the knobs rather than the module constants.
    assert curve.cooldown_ms == 60_000
    assert curve.lambda_t > DEFAULT_CURVE.lambda_t    # shorter half-life = faster decay


def test_a_shorter_half_life_decays_strength_faster():
    now = int(time.time() * 1000)
    thirty_days_ago = now - 30 * 86_400_000
    fast = ReinforceCurve(half_life_days=7.0)
    assert adjust(1.0, thirty_days_ago, now, fast) < adjust(1.0, thirty_days_ago, now, DEFAULT_CURVE)
    # And the default curve is the shipped behaviour, unchanged.
    assert adjust(1.0, thirty_days_ago, now, DEFAULT_CURVE) == adjust(1.0, thirty_days_ago, now)


def test_the_configured_curve_is_the_one_used_for_strength_reads(tmp_path, monkeypatch):
    """A retuned curve changes what the store reports, end to end."""
    async def scenario(half_life: float) -> float:
        mem = _make(tmp_path / f"hl-{half_life}", monkeypatch,
                    reinforce_half_life_days=half_life)
        await mem.start()
        try:
            receipt = await mem.add("u1", "s1", "我的常用颜色是蓝色")
            fact_id = receipt["outcome"]["written"][0]
            # Bank one reinforcement, then age the snapshot a month.
            mem.reinforce("u1", fact_id, kind="user_confirmed", session_id="s_x")
            aged = int(time.time() * 1000) - 30 * 86_400_000
            mem.db.execute(
                "UPDATE facts SET last_used_at = ? WHERE fact_id = ?", (aged, fact_id)
            )
            mem.db.commit()
            return mem.list_facts("u1")["facts"][0]["effective_importance"]
        finally:
            await mem.stop()

    slow = asyncio.run(scenario(365.0))
    fast = asyncio.run(scenario(1.0))
    assert fast <= slow
