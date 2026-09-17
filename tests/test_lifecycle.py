"""Tests for the memory lifecycle: atomic writes, conflict resolution through the
worker, the archive tier, retention, index repair, and the read-through profile.

These are the behaviours that decide what the store *contains* and what a caller
is told about it, so they are asserted end to end (``AtomMem`` + its worker)
rather than against the helpers alone.
"""

from __future__ import annotations

import asyncio
import json

import pytest

from atom_memory import AtomMem, MemConfig
from atom_memory.embedder import serialize_float32
from atom_memory.models import FactCandidate


class _FakeEmbedder:
    """Deterministic 512-dim embedder (matches the vec0 column width)."""

    def __init__(self, **kwargs) -> None:
        pass

    def embed_one(self, text: str) -> bytes:
        return serialize_float32([0.25] * 512)


class _WrongDimEmbedder:
    """Produces 8 dims, so the vec0 insert fails — the F01 reproduction."""

    def __init__(self, **kwargs) -> None:
        pass

    def embed_one(self, text: str) -> bytes:
        return serialize_float32([0.1] * 8)


def _make(tmp_path, monkeypatch, embedder=_FakeEmbedder, **overrides) -> AtomMem:
    monkeypatch.setattr("atom_memory.api.Embedder", embedder)
    kwargs = dict(
        db_path=str(tmp_path / "mem.db"),
        worker_poll_interval_sec=0.02,
        max_retries=1,
        write_ack_timeout_ms=3000,
    )
    kwargs.update(overrides)
    return AtomMem(MemConfig(**kwargs))


def _run(coro):
    return asyncio.run(coro)


def _insert_fact(
    mem, fact_id, subject, predicate, obj, user="u1",
    confidence=0.8, importance=0.6, type="semantic", status="active",
    created_at=None,
):
    from atom_memory.db import now_ms

    mem.db.execute(
        "INSERT INTO facts(fact_id, user_id, session_id, subject, predicate, "
        "object, confidence, importance, source_type, status, observed_at, "
        "created_at, version, type) VALUES (?, ?, 's1', ?, ?, ?, ?, ?, "
        "'user_explicit', ?, 1, ?, 1, ?)",
        (
            fact_id, user, subject, predicate, obj, confidence, importance,
            status, created_at or now_ms() or 2, type,
        ),
    )
    mem.db.commit()


def _events(mem, user="u1"):
    return [
        json.loads(r["payload"])
        for r in mem.db.execute(
            "SELECT payload FROM events WHERE user_id = ? ORDER BY rowid",
            (user,),
        ).fetchall()
    ]


# ---- F01: atomic write + repair ----------------------------------------------

def test_failed_vector_write_leaves_no_partial_fact(tmp_path, monkeypatch):
    """A fact whose vector cannot be written must not exist at all.

    The three writes are one logical fact. When the vector insert failed, the
    row used to survive with its FTS entry — visible to lexical search, invisible
    to semantic search, `active`, and indistinguishable from a healthy fact.
    """
    mem = _make(tmp_path, monkeypatch, embedder=_WrongDimEmbedder)

    async def scenario():
        await mem.start()
        receipt = await mem.add("u1", "s1", "我的常用颜色是蓝色")
        assert receipt["status"] in ("skipped", "error")

        # Nothing half-written is left behind, and the two indexes agree with
        # the facts table (both empty).
        assert mem.db.execute("SELECT COUNT(*) AS n FROM facts").fetchone()["n"] == 0
        assert mem.db.execute("SELECT COUNT(*) AS n FROM facts_fts").fetchone()["n"] == 0
        assert mem.db.execute("SELECT COUNT(*) AS n FROM facts_vec").fetchone()["n"] == 0
        assert mem.index_health()["ok"] is True

        # The failure is reported, not swallowed: the queue row goes dead with
        # the reason, and the audit log carries it.
        for _ in range(200):
            row = mem.db.execute("SELECT status, error FROM task_queue").fetchone()
            if row["status"] == "dead":
                break
            await asyncio.sleep(0.02)
        assert row["status"] == "dead"
        assert "Dimension mismatch" in (row["error"] or "")
        assert mem.db.execute(
            "SELECT COUNT(*) AS n FROM events WHERE type = 'task_dead'"
        ).fetchone()["n"] == 1
        await mem.stop()

    _run(scenario())


def test_maintenance_repairs_a_fact_whose_index_entries_are_missing(tmp_path, monkeypatch):
    """A fact written without its index entries is re-derived, not abandoned."""
    mem = _make(tmp_path, monkeypatch)

    async def scenario():
        await mem.start()
        # Simulate the damage: the row exists, the indexes do not.
        _insert_fact(mem, "orphan", "用户", "职业", "工程师")
        assert mem.index_health()["ok"] is False
        assert mem.index_health()["orphans"]["missing_vector"] == ["orphan"]

        summary = await mem.maintenance("u1")
        assert summary["repaired"]["vector"] == 1
        assert summary["repaired"]["fts"] == 1
        assert mem.index_health()["ok"] is True

        # ...and it is genuinely searchable again.
        found = await mem.recall("u1", "工程师", token_budget=500)
        assert any(f["fact_id"] == "orphan" for f in found["facts"])
        await mem.stop()

    _run(scenario())


def test_maintenance_drops_index_rows_whose_fact_is_gone(tmp_path, monkeypatch):
    mem = _make(tmp_path, monkeypatch)

    async def scenario():
        await mem.start()
        _insert_fact(mem, "f1", "用户", "职业", "工程师")
        mem.db.execute(
            "INSERT INTO facts_vec(fact_id, embedding) VALUES (?, ?)",
            ("ghost", _FakeEmbedder().embed_one("x")),
        )
        mem.db.execute(
            "INSERT INTO facts_fts(fact_id, text) VALUES (?, ?)", ("ghost", "幽灵")
        )
        mem.db.commit()
        summary = await mem.maintenance("u1")
        assert summary["repaired"]["dropped_vector"] == 1
        assert summary["repaired"]["dropped_fts"] == 1
        assert mem.index_health()["ok"] is True
        await mem.stop()

    _run(scenario())


# ---- F05/F06: conflict resolution through the write path ---------------------

def test_newer_value_supersedes_the_stored_one(tmp_path, monkeypatch):
    """A correction must land: "my usual colour is blue" then "…is green"."""
    mem = _make(tmp_path, monkeypatch)

    async def scenario():
        await mem.start()
        first = await mem.add("u1", "s1", "我的常用颜色是蓝色")
        assert first["status"] == "applied"
        assert first["outcome"]["written"], "the first claim is stored"

        second = await mem.add("u1", "s2", "我的常用颜色是绿色")
        superseded = second["outcome"]["superseded"]
        assert len(superseded) == 1, "the newer assertion replaces the stored value"
        assert superseded[0]["old_object"] == "蓝色"
        assert superseded[0]["new_object"] == "绿色"
        assert superseded[0]["reason"] == "newer_assertion"

        active = mem.db.execute(
            "SELECT object FROM facts WHERE status = 'active'"
        ).fetchall()
        assert [r["object"] for r in active] == ["绿色"]
        retired = mem.db.execute(
            "SELECT object, superseded_by FROM facts WHERE status = 'superseded'"
        ).fetchone()
        assert retired["object"] == "蓝色"
        assert retired["superseded_by"] == superseded[0]["new_fact_id"]

        # The decision is auditable.
        assert any("new_object" in e and e["new_object"] == "绿色" for e in _events(mem))
        await mem.stop()

    _run(scenario())


def test_contradiction_from_weaker_evidence_is_refused_and_explained(tmp_path, monkeypatch):
    """A weak claim must not overwrite a strong stored one — and the refusal is
    reported rather than silently swallowed."""
    mem = _make(tmp_path, monkeypatch)

    async def scenario():
        await mem.start()
        _insert_fact(mem, "strong", "用户", "常用颜色", "蓝色",
                     confidence=1.0, importance=1.0)
        receipt = await mem.add("u1", "s1", "我的常用颜色是绿色")
        assert receipt["status"] == "applied"
        assert receipt["reject_kind"] == "conflict"
        assert "outranks" in (receipt["reject_reason"] or "")
        assert receipt["outcome"]["written"] == []
        assert receipt["outcome"]["rejected"][0]["reason"] == "stronger_evidence_stored"

        active = mem.db.execute(
            "SELECT object FROM facts WHERE status = 'active'"
        ).fetchall()
        assert [r["object"] for r in active] == ["蓝色"], "the strong claim survives"
        assert any(e.get("reason") == "stronger_evidence_stored" for e in _events(mem))
        await mem.stop()

    _run(scenario())


def test_restating_the_current_value_is_idempotent_even_with_a_stale_value(
    tmp_path, monkeypatch
):
    """Regression: classifying a claim must not depend on SQLite's row order.

    With a stale competing value in the key, the old single-pass check compared
    the first row it happened to read and reported a conflict, so re-stating the
    *current* value was dropped (and never reinforced) while re-stating the
    *stale* value won the idempotent path.
    """
    mem = _make(tmp_path, monkeypatch)

    async def scenario():
        await mem.start()
        # The stale value is older than the current one.
        _insert_fact(mem, "stale", "用户", "常用颜色", "红色", created_at=100)
        _insert_fact(mem, "current", "用户", "常用颜色", "蓝色", created_at=200)

        from atom_memory.validator import validate

        for expected, obj in (("idempotent", "蓝色"), ("idempotent", "红色")):
            cand = FactCandidate(
                candidate_id="c", user_id="u1", session_id="s",
                subject="用户", predicate="常用颜色", object=obj,
                confidence=0.8, importance=0.6,
            )
            assert validate(cand, mem.db).kind == expected
        await mem.stop()

    _run(scenario())


def test_negation_flip_is_a_correction_not_a_dead_end(tmp_path, monkeypatch):
    """"I like coffee" then "I don't like coffee" is a changed mind, not noise.

    The evidence rule applies uniformly: a flip supersedes the stored claim when
    the new statement's evidence is comparable, and is refused when the stored
    claim is decisively stronger (one rule, not a special case per shape).
    """
    mem = _make(tmp_path, monkeypatch)

    async def scenario():
        await mem.start()
        from atom_memory.worker import _candidate_from_rpc_dict

        def _flip():
            return _candidate_from_rpc_dict(
                {"subject": "用户", "predicate": "偏好", "object": "黑咖啡",
                 "qualifiers": {"negation": True}},
                "u1", "s2", 0,
            )

        _insert_fact(mem, "like", "用户", "偏好", "黑咖啡", confidence=0.5)
        outcome = await mem._worker._apply_candidates([_flip()], "u1", "s2")
        assert outcome["superseded"], "the flipped claim replaces the old one"
        assert outcome["superseded"][0]["old_fact_id"] == "like"

        # Now the stored claim is decisively stronger, so the flip is refused
        # rather than allowed to overwrite it.
        _insert_fact(mem, "strong", "用户", "偏好", "黑咖啡", confidence=1.0,
                     importance=1.0)
        outcome2 = await mem._worker._apply_candidates([_flip()], "u1", "s3")
        assert outcome2["written"] == []
        assert outcome2["rejected"][0]["reason"] == "stronger_evidence_stored"
        await mem.stop()

    _run(scenario())


def test_batch_keeps_one_claim_per_single_valued_key(tmp_path, monkeypatch):
    """Two competing values from one utterance: the stronger wins, the other is
    reported rather than silently absorbed."""
    mem = _make(tmp_path, monkeypatch)

    async def scenario():
        await mem.start()
        weak = FactCandidate(
            candidate_id="weak", user_id="u1", session_id="s",
            subject="用户", predicate="常用颜色", object="绿色",
            confidence=0.4, importance=0.4,
        )
        strong = FactCandidate(
            candidate_id="strong", user_id="u1", session_id="s",
            subject="用户", predicate="常用颜色", object="蓝色",
            confidence=0.9, importance=0.9,
        )
        outcome = await mem._worker._apply_candidates([weak, strong], "u1", "s")
        assert len(outcome["written"]) == 1
        assert len(outcome["rejected"]) == 1
        assert outcome["rejected"][0]["kind"] == "batch_duplicate"
        stored = mem.db.execute(
            "SELECT object FROM facts WHERE status = 'active'"
        ).fetchall()
        assert [r["object"] for r in stored] == ["蓝色"]
        # Multi-valued keys are not deduped: both preferences survive.
        pref_a = FactCandidate(
            candidate_id="a", user_id="u1", session_id="s",
            subject="用户", predicate="偏好", object="黑咖啡",
            confidence=0.8, importance=0.6,
        )
        pref_b = FactCandidate(
            candidate_id="b", user_id="u1", session_id="s",
            subject="用户", predicate="偏好", object="绿茶",
            confidence=0.8, importance=0.6,
        )
        outcome2 = await mem._worker._apply_candidates([pref_a, pref_b], "u1", "s")
        assert len(outcome2["written"]) == 2
        await mem.stop()

    _run(scenario())


def test_two_todos_do_not_overwrite_each_other(tmp_path, monkeypatch):
    """The second to-do is a new item, not a corrected first one.

    Regression for the reported defect: 待办 was read as a single-valued
    attribute, so storing the next to-do superseded the previous one
    (``newer_assertion``) and the user's earlier item disappeared from every
    read path. Both items must stay active.
    """
    mem = _make(tmp_path, monkeypatch)

    async def scenario():
        await mem.start()
        first = FactCandidate(
            candidate_id="t1", user_id="u1", session_id="s",
            subject="dsh-memory", predicate="待办", object="手机真机实测",
            confidence=0.7, importance=0.55, type="task",
        )
        second = FactCandidate(
            candidate_id="t2", user_id="u1", session_id="s",
            subject="dsh-memory", predicate="待办", object="补齐课程大纲",
            confidence=0.7, importance=0.55, type="task",
        )
        one = await mem._worker._apply_candidates([first], "u1", "s")
        two = await mem._worker._apply_candidates([second], "u1", "s")

        assert one["written"] and two["written"]
        assert two["superseded"] == [], "a second to-do must not retire the first"
        active = mem.db.execute(
            "SELECT object FROM facts WHERE status = 'active' ORDER BY created_at"
        ).fetchall()
        assert [r["object"] for r in active] == ["手机真机实测", "补齐课程大纲"]
        await mem.stop()

    _run(scenario())


def test_a_batch_of_todos_all_land(tmp_path, monkeypatch):
    """One utterance listing several to-dos writes every item.

    This is the other half of the defect and the more destructive one: inside a
    single batch the losers were dropped entirely (``batch_duplicate``), so they
    were never stored *and* never superseded — nothing but a line in the write
    receipt said they had ever existed.
    """
    mem = _make(tmp_path, monkeypatch)

    async def scenario():
        await mem.start()
        todos = [
            FactCandidate(
                candidate_id=f"t{i}", user_id="u1", session_id="s",
                subject="用户", predicate="待办", object=item,
                confidence=0.7, importance=0.55, type="task",
            )
            for i, item in enumerate(
                ["新专业申报专班成立", "软著提交", "党务党课讲稿", "课堂质量专项改进"]
            )
        ]
        outcome = await mem._worker._apply_candidates(todos, "u1", "s")
        assert len(outcome["written"]) == 4
        assert outcome["rejected"] == []
        stored = mem.db.execute(
            "SELECT COUNT(*) c FROM facts WHERE status = 'active' AND type = 'task'"
        ).fetchone()["c"]
        assert stored == 4
        await mem.stop()

    _run(scenario())


def test_a_dropped_batch_member_is_recorded_as_an_event(tmp_path, monkeypatch):
    """A claim that never reaches the store still leaves a trace.

    Only genuinely single-valued keys can lose a candidate inside a batch now,
    but that path used to be invisible: the receipt was the only record, and it
    is not queryable after the turn ends.
    """
    mem = _make(tmp_path, monkeypatch)

    async def scenario():
        await mem.start()
        weak = FactCandidate(
            candidate_id="weak", user_id="u1", session_id="s",
            subject="用户", predicate="常用颜色", object="绿色",
            confidence=0.4, importance=0.4,
        )
        strong = FactCandidate(
            candidate_id="strong", user_id="u1", session_id="s",
            subject="用户", predicate="常用颜色", object="蓝色",
            confidence=0.9, importance=0.9,
        )
        await mem._worker._apply_candidates([weak, strong], "u1", "s")
        dropped = [
            e for e in _events(mem)
            if e.get("reason") == "batch_duplicate" and e.get("object") == "绿色"
        ]
        assert len(dropped) == 1
        assert dropped[0]["kept_object"] == "蓝色"
        await mem.stop()

    _run(scenario())


def test_write_ack_timeout_degrades_to_the_enqueue_receipt(tmp_path, monkeypatch):
    """A caller that does not wait still gets an honest receipt."""
    mem = _make(tmp_path, monkeypatch, write_ack_timeout_ms=0)

    async def scenario():
        await mem.start()
        receipt = await mem.add("u1", "s1", "我的常用颜色是蓝色")
        assert receipt["status"] == "pending"
        assert "outcome" not in receipt
        await mem.stop()

    _run(scenario())


def test_candidate_outcome_can_be_pulled_after_the_fact(tmp_path, monkeypatch):
    mem = _make(tmp_path, monkeypatch)

    async def scenario():
        await mem.start()
        receipt = await mem.add("u1", "s1", "我的常用颜色是蓝色")
        assert receipt["status"] == "applied"
        pulled = mem.candidate_outcome(receipt["candidate_id"])
        assert pulled is not None
        assert pulled["outcome"]["written"]
        assert mem.candidate_outcome("nope") is None
        assert mem.recent_outcomes("u1")[0]["candidate_id"] == receipt["candidate_id"]
        await mem.stop()

    _run(scenario())


# ---- F12: purge, archive, retention ------------------------------------------

def test_forget_purge_removes_the_fact_and_its_index_entries(tmp_path, monkeypatch):
    mem = _make(tmp_path, monkeypatch)

    async def scenario():
        await mem.start()
        receipt = await mem.add("u1", "s1", "我的常用颜色是蓝色")
        fact_id = receipt["outcome"]["written"][0]
        purged = await mem.forget("u1", fact_id=fact_id, purge=True)
        assert purged["outcome"]["purged"] == [fact_id]
        assert mem.db.execute("SELECT COUNT(*) AS n FROM facts").fetchone()["n"] == 0
        assert mem.db.execute("SELECT COUNT(*) AS n FROM facts_vec").fetchone()["n"] == 0
        assert mem.db.execute("SELECT COUNT(*) AS n FROM facts_fts").fetchone()["n"] == 0
        await mem.stop()

    _run(scenario())


def test_soft_forget_keeps_the_row_and_reports_what_it_matched(tmp_path, monkeypatch):
    mem = _make(tmp_path, monkeypatch)

    async def scenario():
        await mem.start()
        receipt = await mem.add("u1", "s1", "我的常用颜色是蓝色")
        fact_id = receipt["outcome"]["written"][0]
        retracted = await mem.forget("u1", fact_id=fact_id)
        assert retracted["outcome"]["retracted"] == [fact_id]
        assert "purged" not in retracted["outcome"]
        assert mem.db.execute("SELECT COUNT(*) AS n FROM facts").fetchone()["n"] == 1
        # Forgetting something that is not there says so.
        missing = await mem.forget("u1", fact_id="nope")
        assert missing["outcome"]["rejected"][0]["kind"] == "not_found"
        await mem.stop()

    _run(scenario())


def test_capacity_archives_the_least_valuable_and_can_restore_it(tmp_path, monkeypatch):
    """The cap bounds the working set without destroying anything."""
    mem = _make(tmp_path, monkeypatch, max_active_facts=2, archive_protect_days=0)
    old = 1  # far in the past

    async def scenario():
        await mem.start()
        _insert_fact(mem, "keep_high", "用户", "职业", "工程师",
                     importance=0.95, created_at=old)
        _insert_fact(mem, "keep_mid", "用户", "城市", "天津",
                     importance=0.7, created_at=old)
        _insert_fact(mem, "drop_low", "用户", "爱好", "围棋",
                     importance=0.2, created_at=old)
        archived = await mem.maintenance("u1")
        assert [a["fact_id"] for a in archived["archived"]] == ["drop_low"]
        assert mem.stats("u1")["facts"] == 2
        assert mem.stats("u1")["archived"] == 1
        # Archived rows leave every read path (they are no longer 'active').
        active = {f["fact_id"] for f in mem.list_facts("u1")["facts"]}
        assert active == {"keep_high", "keep_mid"}
        # ...and are recoverable.
        assert mem.unarchive("u1", "drop_low")["restored"] == 1
        assert mem.stats("u1")["facts"] == 3
        with pytest.raises(ValueError):
            mem.unarchive("u1", "not_archived")
        await mem.stop()

    _run(scenario())


def test_capacity_protects_fresh_reinforced_and_durable_facts(tmp_path, monkeypatch):
    """The archive order is a policy: what is *protected* matters more than the
    score, so the pass cannot evict evidence that the fact is valuable."""
    from atom_memory.db import now_ms

    mem = _make(tmp_path, monkeypatch, max_active_facts=1, archive_protect_days=14)
    fresh = now_ms()

    async def scenario():
        await mem.start()
        # Old + low score: the only unprotected, non-durable candidate.
        _insert_fact(mem, "victim", "用户", "爱好", "围棋",
                     importance=0.1, created_at=1)
        # Fresh, so no reuse could have been observed yet.
        _insert_fact(mem, "fresh", "用户", "城市", "天津",
                     importance=0.1, created_at=fresh)
        # Has reuse evidence.
        _insert_fact(mem, "used", "用户", "语言", "Python",
                     importance=0.1, created_at=1)
        mem.db.execute("UPDATE facts SET reinforce_count = 0.5 WHERE fact_id = 'used'")
        # Durable knowledge.
        _insert_fact(mem, "rule", "用户", "决策规则", "先回滚再排查",
                     importance=0.1, created_at=1, type="decision_rule")
        mem.db.commit()

        archived = await mem.maintenance("u1")
        assert [a["fact_id"] for a in archived["archived"]] == ["victim"]
        await mem.stop()

    _run(scenario())


def test_maintenance_prunes_finished_bookkeeping_past_retention(tmp_path, monkeypatch):
    mem = _make(
        tmp_path, monkeypatch,
        candidate_retention_days=1, task_retention_days=1, event_retention_days=1,
    )
    stale = 1  # 1970: far past every retention window

    async def scenario():
        await mem.start()
        mem.db.execute(
            "INSERT INTO fact_candidates(candidate_id, user_id, session_id, "
            "turn_id, status, created_at, finished_at) "
            "VALUES ('c_old','u1','s1',0,'applied',?,?)",
            (stale, stale),
        )
        mem.db.execute(
            "INSERT INTO fact_candidates(candidate_id, user_id, session_id, "
            "turn_id, status, created_at) "
            "VALUES ('c_live','u1','s1',0,'pending',?)",
            (stale,),
        )
        mem.db.execute(
            "INSERT INTO task_queue(task_id, task_type, payload, status, priority, "
            "retry_count, max_retries, created_at, completed_at) "
            "VALUES ('t_old','extract','{}','done',5,0,3,?,?)",
            (stale, stale),
        )
        mem.db.execute(
            "INSERT INTO events(event_id, user_id, type, payload, created_at) "
            "VALUES ('e_old','u1','fact_rejected','{}',?)",
            (stale,),
        )
        mem.db.commit()

        summary = await mem.maintenance("u1")
        assert summary["pruned_candidates"] == 1
        assert summary["pruned_tasks"] == 1
        assert summary["pruned_events"] == 1
        # An unfinished candidate is never collected: it is in-flight work.
        assert mem.db.execute(
            "SELECT COUNT(*) AS n FROM fact_candidates WHERE candidate_id = 'c_live'"
        ).fetchone()["n"] == 1
        await mem.stop()

    _run(scenario())


def test_retention_of_zero_keeps_everything(tmp_path, monkeypatch):
    mem = _make(
        tmp_path, monkeypatch,
        candidate_retention_days=0, task_retention_days=0, event_retention_days=0,
    )

    async def scenario():
        await mem.start()
        mem.db.execute(
            "INSERT INTO fact_candidates(candidate_id, user_id, session_id, "
            "turn_id, status, created_at, finished_at) "
            "VALUES ('c_old','u1','s1',0,'applied',1,1)",
        )
        mem.db.commit()
        summary = await mem.maintenance("u1")
        assert summary["pruned_candidates"] == 0
        assert mem.db.execute(
            "SELECT COUNT(*) AS n FROM fact_candidates"
        ).fetchone()["n"] == 1
        await mem.stop()

    _run(scenario())


# ---- the profile is a table the user owns -------------------------------------

def test_learning_facts_does_not_touch_the_profile(tmp_path, monkeypatch):
    """The reverse of the old read-through behaviour, and the point of the change.

    The profile used to be re-derived from active facts on every read, so
    learning a fact silently created a profile entry and deleting one silently
    came back. Entries now arrive only through the user (accepted suggestions or
    typed rows), and facts are merely *suggestible*.
    """
    mem = _make(tmp_path, monkeypatch)

    async def scenario():
        await mem.start()
        _insert_fact(mem, "f1", "用户", "职业", "工程师")
        _insert_fact(mem, "f2", "用户", "偏好", "黑咖啡")

        page = mem.list_profile("u1")
        assert page["profile"] == [], "facts alone must not create profile entries"

        # ...but they are offered as suggestions, and offering writes nothing.
        offered = mem.profile_candidates("u1")["candidates"]
        assert {(c["section"], c["key"]) for c in offered} == {
            ("偏好", "黑咖啡"), ("职业", "value"),
        }
        assert mem.list_profile("u1")["profile"] == []

        # A deleted entry stays deleted across reads.
        mem.upsert_profile("u1", "职业", "value", "工程师")
        assert mem.delete_profile("u1", "职业", "value")["deleted"] == 1
        for _ in range(3):
            assert mem.list_profile("u1")["profile"] == []
        await mem.stop()

    _run(scenario())
