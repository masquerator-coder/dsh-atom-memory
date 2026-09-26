"""Tests for the UI-facing edit / backup / restore surface of AtomMem.

These methods back the dsh-atom-memory settings panel: paginated fact listing,
direct fact editing (with FTS/vector resync), profile row upsert/delete, and
JSON backup/restore with replace semantics. They deliberately use the fake
embedder so nothing touches model inference.
"""

from __future__ import annotations

import asyncio

import pytest

from atom_memory import AtomMem, MemConfig
from atom_memory.api import _has_conflict_candidate
from atom_memory.backup import BACKUP_VERSION, validate_backup
from atom_memory.embedder import serialize_float32


class _FakeEmbedder:
    def __init__(self, **kwargs) -> None:
        pass

    def embed_one(self, text: str) -> bytes:
        return serialize_float32([0.25] * 512)


def _make(tmp_path, monkeypatch, **overrides) -> AtomMem:
    monkeypatch.setattr("atom_memory.api.Embedder", _FakeEmbedder)
    kwargs = dict(
        db_path=str(tmp_path / "mem.db"),
        worker_poll_interval_sec=0.05,
        max_retries=3,
    )
    kwargs.update(overrides)
    return AtomMem(MemConfig(**kwargs))


def _run(coro):
    return asyncio.run(coro)


def _insert_fact(mem, fact_id, subject, predicate, obj, user="u1", type="semantic", created_at=2):
    mem.db.execute(
        "INSERT INTO facts(fact_id, user_id, session_id, subject, predicate, "
        "object, confidence, importance, source_type, status, observed_at, "
        "created_at, version, type) VALUES (?, ?, 's1', ?, ?, ?, 0.8, 0.6, "
        "'user_explicit', 'active', 1, ?, 1, ?)",
        (fact_id, user, subject, predicate, obj, created_at, type),
    )
    mem.db.commit()


def test_list_facts_paginates(tmp_path, monkeypatch):
    mem = _make(tmp_path, monkeypatch)

    async def scenario():
        await mem.start()
        _insert_fact(mem, "f1", "用户", "职业", "工程师", created_at=1)
        _insert_fact(mem, "f2", "用户", "偏好", "黑咖啡", created_at=2)

        page = mem.list_facts("u1", limit=1, offset=0)
        assert page["total"] == 2
        assert len(page["facts"]) == 1
        # Descending recency: newest (f2, created_at=2) first.
        assert page["facts"][0]["fact_id"] == "f2"

        page2 = mem.list_facts("u1", limit=1, offset=1)
        assert page2["facts"][0]["fact_id"] == "f1"

        # Retracted facts hidden by default.
        mem.db.execute("UPDATE facts SET status='retracted' WHERE fact_id='f2'")
        mem.db.commit()
        assert mem.list_facts("u1")["total"] == 1
        assert mem.list_facts("u1", include_retracted=True)["total"] == 2
        await mem.stop()

    _run(scenario())


def test_edit_fact_updates_spo_and_vectors(tmp_path, monkeypatch):
    mem = _make(tmp_path, monkeypatch)

    async def scenario():
        await mem.start()
        _insert_fact(mem, "f1", "用户", "职业", "工程师")

        updated = await mem.edit_fact("u1", "f1", object="产品经理")
        assert updated["object"] == "产品经理"
        assert updated["subject"] == "用户"

        # FTS/vector resynced to the new text.
        found = await mem.recall("u1", "产品经理", token_budget=2000)
        assert any(f["fact_id"] == "f1" for f in found["facts"])

        with pytest.raises(ValueError):
            await mem.edit_fact("u1", "missing", object="x")
        await mem.stop()

    _run(scenario())


def test_profile_upsert_delete(tmp_path, monkeypatch):
    mem = _make(tmp_path, monkeypatch)

    async def scenario():
        await mem.start()
        mem.upsert_profile("u1", "职业", "value", "工程师")
        page = mem.list_profile("u1")
        rows = page["profile"]
        assert len(rows) == 1
        assert rows[0]["section"] == "职业"
        # `source` records provenance now (this one was typed, not suggested).
        assert rows[0]["source"] == "user"
        assert page["count"] == 1
        assert page["limit"] == 50

        mem.upsert_profile("u1", "职业", "value", "产品经理")
        assert mem.list_profile("u1")["profile"][0]["value"] == "产品经理"

        res = mem.delete_profile("u1", "职业", "value")
        assert res["deleted"] == 1
        assert res["count"] == 0
        assert mem.list_profile("u1")["profile"] == []
        # A delete is permanent now: nothing re-derives the table.
        for _ in range(3):
            assert mem.list_profile("u1")["profile"] == []
        await mem.stop()

    _run(scenario())


def test_learning_a_fact_does_not_create_a_profile_row(tmp_path, monkeypatch):
    """Regression: the panel's delete used to be undone by the next read.

    The profile was a projection rebuilt from active facts on every read, so a
    fact that was still active re-derived the row the user had just deleted.
    Entries now come only from the user.
    """
    mem = _make(tmp_path, monkeypatch)

    async def scenario():
        await mem.start()
        _insert_fact(mem, "f1", "用户", "职业", "工程师", created_at=1)
        mem.db.commit()

        # The fact is remembered, but it is not a profile entry.
        assert mem.list_facts("u1")["total"] == 1
        assert mem.list_profile("u1")["profile"] == []

        # It is offered as a suggestion instead — and offering writes nothing.
        offered = mem.profile_candidates("u1")
        assert offered["candidates"] == [
            {"section": "职业", "key": "value", "value": "工程师"}
        ]
        assert offered["remaining"] == 50
        assert mem.list_profile("u1")["profile"] == []

        # Accept it, delete it, and the fact no longer brings it back.
        mem.upsert_profile("u1", "职业", "value", "工程师")
        assert mem.delete_profile("u1", "职业", "value")["deleted"] == 1
        assert mem.list_profile("u1")["profile"] == []

        # Suggestions remain available, but never re-enter the table on their own.
        assert mem.profile_candidates("u1")["candidates"] != []
        assert mem.list_profile("u1")["profile"] == []
        await mem.stop()

    _run(scenario())


def test_profile_rows_are_capped(tmp_path, monkeypatch):
    """The table is bounded: it is rendered into the prompt on every request."""
    from atom_memory.profile import ProfileLimitExceeded

    mem = _make(tmp_path, monkeypatch, max_profile_rows=3)

    async def scenario():
        await mem.start()
        for index in range(3):
            mem.upsert_profile("u1", f"属性{index}", "value", f"值{index}")
        assert mem.list_profile("u1")["count"] == 3

        with pytest.raises(ProfileLimitExceeded) as excinfo:
            mem.upsert_profile("u1", "溢出", "value", "不行")
        assert excinfo.value.limit == 3
        assert excinfo.value.current == 4
        # The refused write changed nothing.
        assert mem.list_profile("u1")["count"] == 3

        # Editing an existing row is still allowed at the cap.
        mem.upsert_profile("u1", "属性0", "value", "改过的值")
        rows = {r["section"]: r["value"] for r in mem.list_profile("u1")["profile"]}
        assert rows["属性0"] == "改过的值"

        # Deleting frees a slot again.
        mem.delete_profile("u1", "属性1", "value")
        mem.upsert_profile("u1", "新属性", "value", "现在可以了")
        assert mem.list_profile("u1")["count"] == 3
        await mem.stop()

    _run(scenario())


def test_write_profile_batch_is_all_or_nothing(tmp_path, monkeypatch):
    """A batch that breaks the cap leaves the table untouched."""
    from atom_memory.profile import ProfileLimitExceeded

    mem = _make(tmp_path, monkeypatch, max_profile_rows=2)

    async def scenario():
        await mem.start()
        mem.upsert_profile("u1", "职业", "value", "工程师")
        before = mem.list_profile("u1")["profile"]

        with pytest.raises(ProfileLimitExceeded):
            mem.write_profile("u1", [
                {"section": "城市", "key": "value", "value": "天津"},
                {"section": "语言", "key": "value", "value": "中文"},
            ])
        assert mem.list_profile("u1")["profile"] == before

        # A batch that deletes one row and adds one fits, and applies both.
        result = mem.write_profile("u1", [
            {"section": "职业", "key": "value", "value": "", "deleted": True},
            {"section": "城市", "key": "value", "value": "天津"},
        ])
        assert result["deleted"] == 1
        assert result["written"] == 1
        rows = {r["section"]: r["value"] for r in mem.list_profile("u1")["profile"]}
        assert rows == {"城市": "天津"}

        # A row missing its identity is refused outright.
        with pytest.raises(ValueError):
            mem.write_profile("u1", [{"section": "", "key": "value", "value": "x"}])
        await mem.stop()

    _run(scenario())


def test_profile_writes_strip_invisible_characters(tmp_path, monkeypatch):
    """Both profile write paths sanitise, so the store never holds a hidden instruction.

    A profile value is rendered for the model, so it is model-visible text and
    falls under the same ingest rule as a fact body. The batch path used to
    collapse whitespace only, which left bidi overrides and zero-width
    characters intact — the exact "instruction a human reviewer cannot see"
    shape. The host fences the profile on the way out too, but a defence that
    depends on every caller remembering to apply it is not a structural one.
    """
    mem = _make(tmp_path, monkeypatch)
    # U+202E (RLO) reorders how a line reads; U+200B is invisible; U+2066 (LRI)
    # is a bidi isolate. All are category Cf and most are not on the host's
    # hand-maintained list, so the ingest layer is the one that must remove them.
    hostile = "工程\u202e师\u200b\u2066值"

    async def scenario():
        await mem.start()

        # The batch path (the settings panel's one "save all").
        mem.write_profile("u1", [{"section": "职业", "key": "value", "value": hostile}])
        rows = {r["section"]: r["value"] for r in mem.list_profile("u1")["profile"]}
        assert rows["职业"] == "工程师值"

        # The single-row path reports what it cleaned, and stores the same text.
        mem.upsert_profile("u1", "城市", "value", hostile)
        rows = {r["section"]: r["value"] for r in mem.list_profile("u1")["profile"]}
        assert rows["城市"] == "工程师值"

        # Section and key are headlines: they sanitise and stay single-line.
        mem.write_profile("u1", [{"section": f"备注{hostile}\n下一行", "key": "k", "value": "v"}])
        sections = [r["section"] for r in mem.list_profile("u1")["profile"]]
        assert any(s == "备注工程师值 下一行" for s in sections), sections

        # The rendered form the model would read carries no invisible character.
        for row in mem.list_profile("u1")["profile"]:
            for ch in row["section"] + row["key"] + row["value"]:
                assert ch not in "\u202e\u200b\u2066\u200e\u200f\u2069"
        await mem.stop()

    _run(scenario())


def test_backup_restore_roundtrip(tmp_path, monkeypatch):
    mem = _make(tmp_path, monkeypatch)

    async def scenario():
        await mem.start()
        _insert_fact(mem, "f1", "用户", "职业", "工程师")
        _insert_fact(mem, "f2", "用户", "偏好", "黑咖啡")
        mem.upsert_profile("u1", "职业", "value", "工程师")

        snapshot = mem.backup("u1")
        assert snapshot["version"] == BACKUP_VERSION
        assert len(snapshot["facts"]) == 2
        assert len(snapshot["profile"]) == 1

        # Change memory, then restore the snapshot back (replace semantics).
        mem.db.execute("UPDATE facts SET status='retracted' WHERE user_id='u1'")
        mem.db.execute("DELETE FROM user_profile WHERE user_id='u1'")
        mem.db.commit()

        validate_backup(snapshot)
        restored = await mem.restore("u1", snapshot)
        assert restored["facts_written"] == 2
        assert mem.list_facts("u1")["total"] == 2
        # The profile round-trips exactly: it is a table the user owns, not a
        # view of the restored facts, so the snapshot's own rows are what come
        # back — restoring must not invent entries from the facts.
        rows = mem.list_profile("u1")["profile"]
        assert [r["section"] for r in rows] == ["职业"]
        assert rows[0]["source"] == "user"
        await mem.stop()

    _run(scenario())


def test_backup_roundtrip_preserves_row_provenance(tmp_path, monkeypatch):
    """A restore must not relabel what the user wrote as suggested, or vice versa."""
    mem = _make(tmp_path, monkeypatch)

    async def scenario():
        await mem.start()
        mem.upsert_profile("u1", "职业", "value", "工程师")
        mem.write_profile("u1", [{"section": "城市", "key": "value", "value": "天津"}])
        # Tag the second row as coming from an accepted suggestion.
        mem.db.execute(
            "UPDATE user_profile SET source='generated' WHERE section='城市'"
        )
        mem.db.commit()

        snapshot = mem.backup("u1")
        sources = {r["section"]: r["source"] for r in snapshot["profile"]}
        assert sources == {"职业": "user", "城市": "generated"}

        mem.db.execute("DELETE FROM user_profile WHERE user_id='u1'")
        mem.db.commit()
        await mem.restore("u1", snapshot)

        after = {r["section"]: r["source"] for r in mem.list_profile("u1")["profile"]}
        assert after == {"职业": "user", "城市": "generated"}
        await mem.stop()

    _run(scenario())


def test_export_memory_is_valueless_without_rows(tmp_path, monkeypatch):
    mem = _make(tmp_path, monkeypatch)

    async def scenario():
        await mem.start()
        snapshot = mem.backup("u1")
        assert snapshot["facts"] == []
        assert snapshot["profile"] == []
        await mem.stop()

    _run(scenario())


def test_validate_backup_rejects_bad_shape():
    with pytest.raises(ValueError):
        validate_backup({"version": 999, "facts": [], "profile": []})
    with pytest.raises(ValueError):
        validate_backup({"version": BACKUP_VERSION, "facts": "nope", "profile": []})


def test_recall_reports_single_valued_conflicts(tmp_path, monkeypatch):
    """recall() surfaces contradictory active facts under a single-valued
    predicate, instead of the old hardcoded empty ``conflicts`` stub."""
    mem = _make(tmp_path, monkeypatch)

    async def scenario():
        await mem.start()
        # Same (用户, 职业) — single-valued attribute — with two different
        # objects: a contradiction recall should report.
        _insert_fact(mem, "f_a", "用户", "职业", "工程师", created_at=1)
        _insert_fact(mem, "f_b", "用户", "职业", "设计师", created_at=2)
        # A multi-valued preference with two objects is NOT a conflict.
        _insert_fact(mem, "f_like1", "用户", "偏好", "黑咖啡")
        _insert_fact(mem, "f_like2", "用户", "偏好", "绿茶")

        r = await mem.recall("u1", "职业", token_budget=2000)
        conflicts = r["conflicts"]
        assert len(conflicts) == 1, "one contradictory pair reported"
        pair = conflicts[0]
        assert pair["subject"] == "用户"
        assert pair["predicate"] == "职业"
        assert {pair["left"], pair["right"]} == {"f_a", "f_b"}
        assert {pair["object_left"], pair["object_right"]} == {"工程师", "设计师"}

        # The preference objects must not have leaked into conflicts.
        csub = {(c["left"], c["right"]) for c in conflicts}
        assert ("f_like1", "f_like2") not in csub
        await mem.stop()

    _run(scenario())


def test_recall_conflicts_ignore_retracted_and_multi_valued(tmp_path, monkeypatch):
    """Conflicts only consider active rows, and only under single-valued keys."""
    mem = _make(tmp_path, monkeypatch)

    async def scenario():
        await mem.start()
        _insert_fact(mem, "f1", "用户", "职业", "工程师", created_at=1)
        _insert_fact(mem, "f2", "用户", "职业", "设计师", created_at=2)
        # Retracted duplicate must not be reported.
        _insert_fact(mem, "f_old", "用户", "职业", "经理", created_at=3)
        mem.db.execute("UPDATE facts SET status='retracted' WHERE fact_id='f_old'")
        # A knowledge (multi-valued) item sharing a predicate is independent.
        _insert_fact(mem, "f_k1", "用户", "发布流程", "A", type="sop")
        _insert_fact(mem, "f_k2", "用户", "发布流程", "B", type="sop")
        _insert_fact(mem, "f_ev1", "用户", "事件", "发布完成", type="episodic")
        _insert_fact(mem, "f_ev2", "用户", "事件", "回滚", type="episodic")
        mem.db.commit()

        r = await mem.recall("u1", "职业", token_budget=2000)
        pairs = {(c["left"], c["right"]) for c in r["conflicts"]}
        assert len(r["conflicts"]) == 1, "only the single-valued 职业 pair"
        assert ("f1", "f2") in pairs
        assert not any("f_old" in p for p in pairs)
        assert not any("发布流程" in c["predicate"] for c in r["conflicts"])
        assert not any("事件" in c["predicate"] for c in r["conflicts"])
        await mem.stop()

    _run(scenario())


# -- lazy conflict gate -------------------------------------------------------
#
# `_load_conflicts` runs on every recall, and its full scan (every active row
# plus a batched scope lookup) dominated the read path's non-retrieval cost. It
# is now gated by `_has_conflict_candidate`, a cheap necessary condition: no
# (subject, predicate) key holds two distinct objects => no scope-respecting
# grouping can find a conflict. These tests pin both directions of that gate,
# because a gate that is merely *fast* is worthless if it is also *wrong*.


def test_conflict_gate_short_circuits_on_a_clean_store(tmp_path, monkeypatch):
    """The gate must say "impossible" — not merely "none found" — when clean.

    This is the performance half of the contract: on a store where no
    single-valued key holds two objects, the expensive scope-aware scan must not
    run at all. Asserted directly on the gate so the test fails if the guard is
    ever bypassed, rather than only observing that the answer is still `[]`.
    """
    mem = _make(tmp_path, monkeypatch)

    async def scenario():
        await mem.start()
        _insert_fact(mem, "f1", "用户", "职业", "工程师", created_at=1)
        # Two *distinct* single-valued keys, each with one object: no pair
        # anywhere, so no scope partition can produce one.
        _insert_fact(mem, "f2", "用户", "城市", "天津", created_at=2)
        # Multi-valued keys with several objects are independent by definition
        # and must not arm the gate either.
        _insert_fact(mem, "f3", "用户", "偏好", "黑咖啡", created_at=3)
        _insert_fact(mem, "f4", "用户", "偏好", "绿茶", created_at=4)
        mem.db.commit()

        rows = mem._single_valued_rows("u1")
        assert _has_conflict_candidate(rows, mem.config) is False, (
            "clean store must short-circuit before the scope-aware scan"
        )
        assert mem._load_conflicts("u1") == []
        assert (await mem.recall("u1", "职业"))["conflicts"] == []
        await mem.stop()

    _run(scenario())


def test_conflict_gate_defers_when_objects_are_split_across_scopes(
    tmp_path, monkeypatch,
):
    """A true gate answer is *inconclusive* and must fall through to the scan.

    Two objects under one single-valued key are exactly what arms the gate, but
    if they live in different scopes they are not a conflict. The gate must not
    report anything itself — it only decides whether the real check may be
    skipped. This pins the asymmetry: `False` is a proof, `True` is a deferral.
    """
    mem = _make(tmp_path, monkeypatch)

    async def scenario():
        await mem.start()
        _insert_fact(mem, "g1", "用户", "职业", "工程师", created_at=1)
        _insert_fact(mem, "g2", "用户", "职业", "设计师", created_at=2)
        mem.db.commit()

        rows = mem._single_valued_rows("u1")
        assert _has_conflict_candidate(rows, mem.config) is True, (
            "two objects under one key must arm the gate"
        )
        # Unbound facts are global facts, so they share a scope set and really
        # do conflict — the gate's deferral must reach that conclusion.
        pairs = {(c["left"], c["right"]) for c in mem._load_conflicts("u1")}
        assert pairs == {("g1", "g2")}
        await mem.stop()

    _run(scenario())


def test_conflict_gate_ignores_multi_valued_objects(tmp_path, monkeypatch):
    """Multi-valued predicates never arm the gate, however many objects they hold.

    The filter has to happen before grouping: a preference list with three
    values is a three-way "conflict" to a naive grouper, which would arm the
    gate on essentially every real store and silently undo the optimisation.
    """
    mem = _make(tmp_path, monkeypatch)

    async def scenario():
        await mem.start()
        for i, value in enumerate(("黑咖啡", "绿茶", "乌龙"), start=1):
            _insert_fact(mem, f"m{i}", "用户", "偏好", value, created_at=i)
        for i, value in enumerate(("A", "B"), start=1):
            _insert_fact(mem, f"k{i}", "用户", "发布流程", value, type="sop", created_at=i)
        mem.db.commit()

        assert mem._single_valued_rows("u1") == []
        assert _has_conflict_candidate(mem._single_valued_rows("u1"), mem.config) is False
        assert mem._load_conflicts("u1") == []
        await mem.stop()

    _run(scenario())


def test_is_multi_valued_reflects_the_conflict_rule():
    from atom_memory.validator import is_multi_valued

    # Collection predicates and episodic/knowledge types are multi-valued.
    assert is_multi_valued("偏好", "semantic") is True
    assert is_multi_valued("职业", "episodic") is True
    assert is_multi_valued("职业", "sop") is True
    # An ordinary single-valued attribute is not.
    assert is_multi_valued("职业", "semantic") is False
