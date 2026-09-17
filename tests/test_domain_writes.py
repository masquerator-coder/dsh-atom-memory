"""The topic dimension on the write path, and its public surface.

`tests/test_domain.py` covers the vocabulary itself. This file covers what the
store *does* with it when a fact is written:

* every stored fact carries a topic, and the rule that chose it is recorded
  (``source``), because a label nobody can explain is a label nobody can correct;
* a topic the vocabulary does not hold becomes its nearest registered ancestor
  and a queue entry, never a new name
  (``test_an_unregistered_hint_falls_back_to_its_ancestor``);
* the extractor's own ``primary_domain`` survives the cap, even when the model
  listed it last — the ordering the cap must not be allowed to break
  (``test_the_extractors_primary_survives_the_cap``);
* ``domain_tagging_mode="off"`` stores nothing, so phase one of the rollout can
  ship without changing a single read.
"""

from __future__ import annotations

import asyncio
import hashlib
import uuid

import pytest

from atom_memory import AtomMem, MemConfig
from atom_memory.db import connect_for_tests, now_ms
from atom_memory.embedder import serialize_float32
from atom_memory.rpc import RpcServer
from atom_memory.worker import _candidate_from_rpc_dict, Worker

PROJECT = {"signals": {"git_remote": "git@github.com:acme/course.git",
                       "git_root": "D:/work/course"}}


class _FakeEmbedder:
    """Stand-in for the model: 512 deterministic floats, no inference."""

    def __init__(self, **kwargs) -> None:
        pass

    def embed_one(self, text: str) -> bytes:
        digest = hashlib.sha256(text.encode("utf-8")).digest()
        return serialize_float32([b / 255.0 for b in (digest * 16)[:512]])


def embed(text: str) -> bytes:
    digest = hashlib.sha256(text.encode("utf-8")).digest()
    return (digest * 64)[: 512 * 4]


def _worker(conn, config):
    return Worker(conn=conn, embed_func=embed, config=config)


def _candidate(subject, predicate, obj, **kwargs):
    payload = {
        "subject": subject, "predicate": predicate, "object": obj,
        "type": kwargs.pop("type", "semantic"),
        "confidence": kwargs.pop("confidence", 0.9),
        "importance": kwargs.pop("importance", 0.9),
    }
    payload.update(kwargs)
    return _candidate_from_rpc_dict(payload, "u", "s1", 0)


def _write(worker, subject, predicate, obj, scope_context=None, **kwargs):
    """Run one candidate through the write gate and return its outcome."""
    async def scenario():
        return await worker._apply_candidates(
            [_candidate(subject, predicate, obj, **kwargs)], "u", "s1",
            scope_context=scope_context,
        )
    return asyncio.run(scenario())


# -- write path ---------------------------------------------------------------


def test_a_write_is_labelled_and_reports_the_rule_that_chose_it():
    config = MemConfig(scope_aware=True)
    conn = connect_for_tests(config)
    try:
        worker = _worker(conn, config)
        # The vocabularies are per user; seed the one this write will use.
        worker.domain_store().create("u", "teaching")
        outcome = _write(
            worker, "用户", "偏好", "先讲概念再举例",
            scope_context=PROJECT,
            domain_hints=["teaching"],
        )
        assert len(outcome["written"]) == 1
        assignment = outcome["domains"][0]
        assert assignment["primary"] == "teaching"
        assert assignment["domains"][0]["source"] == "hint"

        fact_id = outcome["written"][0]
        rows = conn.execute(
            "SELECT domain_id, is_primary, source FROM fact_domain WHERE fact_id = ?",
            (fact_id,),
        ).fetchall()
        assert len(rows) == 1 and rows[0]["is_primary"] == 1
        assert rows[0]["source"] == "hint"
    finally:
        conn.close()


def test_the_labels_land_in_the_facts_own_transaction():
    """A fact whose topic was decided but not stored would read as unlabelled,
    which every compatibility rule has to treat as "visible to all topics"."""
    config = MemConfig(scope_aware=True)
    conn = connect_for_tests(config)
    try:
        worker = _worker(conn, config)
        worker.domain_store().create("u", "teaching")
        outcome = _write(
            worker, "用户", "偏好", "喜欢用例子讲", scope_context=PROJECT,
            domain_hints=["teaching"],
        )
        fact_id = outcome["written"][0]
        count = conn.execute(
            "SELECT COUNT(*) AS n FROM fact_domain WHERE fact_id = ?", (fact_id,)
        ).fetchone()["n"]
        assert count == 1
        # ...and the row is reachable through the index the reads will use.
        assert conn.execute(
            "SELECT COUNT(*) AS n FROM fact_domain fd "
            "JOIN domain d ON d.id = fd.domain_id WHERE d.canonical_name = 'teaching'"
        ).fetchone()["n"] == 1
    finally:
        conn.close()


def test_general_is_only_used_when_nothing_else_can_label_the_fact():
    config = MemConfig(scope_aware=True)
    conn = connect_for_tests(config)
    try:
        worker = _worker(conn, config)
        outcome = _write(worker, "用户", "偏好", "喜欢黑咖啡")
        assignment = outcome["domains"][0]
        assert assignment["primary"] == "general"
        assert assignment["domains"][0]["source"] == "session_default"
    finally:
        conn.close()


def test_an_unregistered_hint_falls_back_to_its_ancestor():
    """The registry rule on the write path: a name the vocabulary does not hold
    is filed under its nearest registered ancestor *and queued*, so the fact is
    filterable immediately and the new name is still the user's decision."""
    config = MemConfig(scope_aware=True)
    conn = connect_for_tests(config)
    try:
        worker = _worker(conn, config)
        store = worker.domain_store()
        store.create("u", "teaching")
        outcome = _write(
            worker, "用户", "偏好", "每章配三个练习",
            domain_hints=["teaching/ds/ch3"],
        )
        assignment = outcome["domains"][0]
        assert assignment["primary"] == "teaching"
        assert assignment["unregistered"] == ["teaching/ds/ch3", "teaching/ds"]
        assert store.find("u", "teaching/ds/ch3") is None, (
            "a proposal never registers itself"
        )
        assert [s.canonical_name for s in store.unresolved("u")] == [
            "teaching/ds", "teaching/ds/ch3",
        ]
    finally:
        conn.close()


def test_the_extractors_primary_survives_the_cap():
    """The cap drops from the tail, so the primary has to be moved to the front
    *before* it is applied. Capping first threw away a primary the model had
    deliberately listed last — the one ordering the cap must not break."""
    config = MemConfig(scope_aware=True, domain_max_per_hint=2)
    conn = connect_for_tests(config)
    try:
        worker = _worker(conn, config)
        store = worker.domain_store()
        for name in ("a", "b", "teaching"):
            store.create("u", name)
        outcome = _write(
            worker, "用户", "偏好", "先讲概念再举例",
            domain_hints=["a", "b", "teaching"],
            primary_domain="teaching",
        )
        assignment = outcome["domains"][0]
        assert assignment["primary"] == "teaching"
        assert [label["name"] for label in assignment["domains"]] == ["teaching", "a"]
    finally:
        conn.close()


def test_a_restatement_merges_labels_instead_of_dropping_them():
    """A repeat is not a new row, but it *is* a second observation of the topic:
    folding the duplicate away must not discard the label that came with it."""
    config = MemConfig(scope_aware=True)
    conn = connect_for_tests(config)
    try:
        worker = _worker(conn, config)
        store = worker.domain_store()
        store.create("u", "teaching")
        store.create("u", "programming")
        first = _write(worker, "用户", "偏好", "先讲概念再举例",
                       scope_context=PROJECT, domain_hints=["teaching"])
        fact_id = first["written"][0]
        second = _write(worker, "用户", "偏好", "先讲概念再举例",
                        scope_context=PROJECT, domain_hints=["programming"])
        assert second["reinforced"], "the identical claim folds into the existing row"
        labels = {label.name: label for label in store.labels_of(fact_id)}
        assert set(labels) == {"teaching", "programming"}
        assert labels["teaching"].is_primary, "the first label keeps its standing"
    finally:
        conn.close()


def test_tagging_mode_off_stores_no_labels():
    """Phase one ships with this switch: labels can be inspected before they are
    allowed to influence anything, and a deployment that wants none keeps the
    pre-domain behaviour byte for byte."""
    config = MemConfig(scope_aware=True, domain_tagging_mode="off")
    conn = connect_for_tests(config)
    try:
        worker = _worker(conn, config)
        outcome = _write(
            worker, "用户", "偏好", "先讲概念再举例",
            scope_context=PROJECT, domain_hints=["teaching"],
        )
        assert outcome["written"]
        assert outcome["domains"][0]["domains"] == []
        assert conn.execute("SELECT COUNT(*) AS n FROM fact_domain").fetchone()["n"] == 0
        assert conn.execute("SELECT COUNT(*) AS n FROM domain").fetchone()["n"] == 0
    finally:
        conn.close()


def test_the_scope_mapping_labels_a_fact_the_extractor_said_nothing_about():
    """The rule that keeps `general` from becoming the default bucket."""
    conn = None
    config = MemConfig(scope_aware=True)
    conn = connect_for_tests(config)
    try:
        from atom_memory.scope import ScopeStore

        worker = _worker(conn, config)
        scope_store = ScopeStore(conn, config)
        mapped = MemConfig(
            scope_aware=True,
            scope_domain_map=((scope_store.path_of(1), "work"),),
        )
        worker.config = mapped
        outcome = _write(worker, "项目", "规范", "提交前跑测试", scope_context=PROJECT)
        assignment = outcome["domains"][0]
        assert assignment["primary"] == "work"
        assert assignment["domains"][0]["source"] == "scoped_map"
    finally:
        conn.close()


# -- public surface -----------------------------------------------------------


def _mem(tmp_path, monkeypatch, **overrides) -> AtomMem:
    monkeypatch.setattr("atom_memory.api.Embedder", _FakeEmbedder)
    kwargs = dict(
        db_path=str(tmp_path / "mem.db"),
        worker_poll_interval_sec=0.05,
        max_retries=3,
    )
    kwargs.update(overrides)
    return AtomMem(MemConfig(**kwargs))


def _fact(mem, fact_id="f1", user="u"):
    ts = now_ms()
    mem.db.execute(
        "INSERT INTO facts(fact_id, user_id, session_id, subject, predicate, object, "
        "observed_at, created_at) VALUES (?, ?, 's', '用户', '偏好', '黑咖啡', ?, ?)",
        (fact_id, user, ts, ts),
    )
    mem.db.commit()
    return fact_id


def test_the_topic_surface_creates_inspects_and_corrects(tmp_path, monkeypatch):
    mem = _mem(tmp_path, monkeypatch)

    async def scenario():
        await mem.start()
        try:
            created = mem.domain_create(
                "u", "teaching/ds", display_name="数据结构教学"
            )
            assert created["path"] == "teaching/ds"
            assert created["display_name"] == "数据结构教学"
            # Missing ancestors are created, so the tree is complete.
            assert [row["name"] for row in mem.domain_list("u")] == [
                "teaching", "teaching/ds",
            ]

            fact_id = _fact(mem)
            assert mem.fact_domain_get("u", fact_id)["domains"] == []
            stored = mem.fact_domain_set("u", fact_id, ["teaching/ds", "teaching"])
            assert stored["primary"] == "teaching/ds"
            assert [d["name"] for d in stored["domains"]] == ["teaching/ds", "teaching"]
            assert all(d["source"] == "user_explicit" for d in stored["domains"])

            detail = mem.fact_domain_get("u", fact_id)
            assert detail["primary"] == "teaching/ds"

            # Isolation: another user cannot touch either the fact or the label.
            with pytest.raises(ValueError):
                mem.fact_domain_set("someone-else", fact_id, ["teaching"])
        finally:
            await mem.stop()

    asyncio.run(scenario())


def test_domain_resolve_explains_without_creating(tmp_path, monkeypatch):
    """A diagnostic call must not grow the vocabulary — the same rule the scope
    resolver follows for a query."""
    mem = _mem(tmp_path, monkeypatch)

    async def scenario():
        await mem.start()
        try:
            mem._domain_store().seed_from_scopes("u")
            mem.domain_create("u", "teaching")
            resolved = mem.domain_resolve(
                "u", ["teaching/ds/ch3"], scope_context=PROJECT
            )
            assert resolved["session"]["names"] == ["general"], (
                "no mapping, no label: the last fallback is what the session has"
            )
            assert resolved["proposals"][0]["resolved"] == "teaching"
            assert resolved["proposals"][0]["ancestors"] == []
            assert resolved["unresolved"] == ["teaching/ds/ch3", "teaching/ds"]
            assert [row["name"] for row in mem.domain_list("u")] == [
                "general", "teaching", "user",
            ], "resolve reported the tree but changed nothing"
            assert mem.domain_unresolved("u") == [], "resolve creates no queue entries"
        finally:
            await mem.stop()

    asyncio.run(scenario())


def test_domain_merge_and_rename_keep_labels_readable(tmp_path, monkeypatch):
    mem = _mem(tmp_path, monkeypatch)

    async def scenario():
        await mem.start()
        try:
            ds = mem.domain_create("u", "teaching/ds")
            teaching = mem.domain_create("u", "teaching")
            fact_id = _fact(mem)
            mem.fact_domain_set("u", fact_id, ["teaching/ds"])

            renamed = mem.domain_rename("u", teaching["domain_id"], "pedagogy")
            assert renamed["to"] == "pedagogy"
            assert mem.fact_domain_get("u", fact_id)["primary"] == "pedagogy/ds"

            merged = mem.domain_merge("u", ds["domain_id"], teaching["domain_id"])
            assert merged["labels_moved"] == 1
            assert mem.fact_domain_get("u", fact_id)["primary"] == "pedagogy", (
                "a merged label reads through the merge without touching the fact"
            )
        finally:
            await mem.stop()

    asyncio.run(scenario())


def test_domain_bridges_and_the_registration_queue(tmp_path, monkeypatch):
    mem = _mem(tmp_path, monkeypatch)

    async def scenario():
        await mem.start()
        try:
            teaching = mem.domain_create("u", "teaching")
            programming = mem.domain_create("u", "programming")
            bridge = mem.domain_bridge_add(
                "u", teaching["domain_id"], programming["domain_id"], 0.6
            )
            assert bridge["added"] is True

            fact_id = _fact(mem)
            mem.fact_domain_set("u", fact_id, ["teaching"])
            # A queued proposal can be rejected or registered; either way it
            # stops being offered.
            mem._domain_store().record_signal("u", "life/travel")
            assert [s["name"] for s in mem.domain_unresolved("u")] == ["life/travel"]
            promoted = mem.domain_signal_promote("u", "life/travel", "旅行")
            assert promoted["name"] == "life/travel"
            assert mem.domain_unresolved("u") == []

            mem._domain_store().record_signal("u", "hobby")
            rejected = mem.domain_signal_reject("u", "hobby")
            assert rejected["rejected"] == 1
            assert mem.domain_unresolved("u") == []
        finally:
            await mem.stop()

    asyncio.run(scenario())


class _StubMem:
    """Records the calls the RPC layer makes, and answers like the real one."""

    def __init__(self) -> None:
        self.calls: list = []

    def __getattr__(self, name):
        def _call(**params):
            self.calls.append((name, params))
            return {"method": name, "params": params}
        return _call


def test_every_domain_rpc_method_is_reachable_and_forwards_its_params():
    """The RPC layer is a name table: a typo there is a feature that exists in
    the library and is unreachable over the wire."""
    server = RpcServer()
    server._started = True
    server.mem = _StubMem()

    async def scenario():
        requests = [
            ("domain_list", {"user_id": "u"}),
            ("domain_resolve", {"user_id": "u", "labels": ["teaching"],
                                "scope_context": PROJECT}),
            ("domain_create", {"user_id": "u", "name": "teaching"}),
            ("domain_rename", {"user_id": "u", "domain_id": 1, "name": "pedagogy"}),
            ("domain_merge", {"user_id": "u", "from_id": 1, "to_id": 2}),
            ("domain_archive", {"user_id": "u", "domain_id": 1}),
            ("domain_bridge_add", {"user_id": "u", "from_id": 1, "to_id": 2}),
            ("domain_unresolved", {"user_id": "u"}),
            ("domain_signal_promote", {"user_id": "u", "name": "x"}),
            ("domain_signal_reject", {"user_id": "u", "name": "x"}),
            ("fact_domain_set", {"user_id": "u", "fact_id": "f",
                                 "domains": ["teaching"]}),
            ("fact_domain_get", {"user_id": "u", "fact_id": "f"}),
        ]
        return [await server._dispatch(method, params) for method, params in requests]

    results = asyncio.run(scenario())
    assert [r["method"] for r in results] == [
        "domain_list", "domain_resolve", "domain_create", "domain_rename",
        "domain_merge", "domain_archive", "domain_bridge_add", "domain_unresolved",
        "domain_signal_promote", "domain_signal_reject", "fact_domain_set",
        "fact_domain_get",
    ]
    forwarded = dict(server.mem.calls)
    assert forwarded["domain_resolve"]["scope_context"] == PROJECT
    assert forwarded["fact_domain_set"]["domains"] == ["teaching"]


def test_the_first_write_seeds_the_vocabulary_from_the_scope_tree(
    tmp_path, monkeypatch
):
    """The starting vocabulary is derived, not shipped.

    A store that already has a project scope knows what that project is called —
    so the first fact written under it becomes about a topic named after the
    project, without anyone configuring a vocabulary first. That is what makes the
    *second* file of a course findable from the first, and it is the reason
    seeding is not a static list of root topics.
    """
    mem = _mem(tmp_path, monkeypatch)

    async def scenario():
        await mem.start()
        try:
            mem.scope_resolve(
                "u",
                {"signals": {"git_remote": "git@github.com:acme/course.git",
                             "git_root": "D:/work/course"}},
                create=True,
            )
            # A fact row is the only thing that records who owns the scope tree.
            # Tagging is off, so it is stored unlabelled — phase one: labels exist
            # and can be inspected before they are allowed to filter anything.
            _fact(mem, fact_id="f1")
            assert [row["name"] for row in mem.domain_list("u")] == []

            # The next start (or any write) seeds the vocabulary: this is the
            # first moment the owner of the scope tree is actually known — a
            # scope carries no user of its own.
            mem._seed_domains()
            names = [row["name"] for row in mem.domain_list("u")]
            assert "github.com/acme/course" in names
            assert "user" in names
            assert "general" in names
        finally:
            await mem.stop()

    asyncio.run(scenario())


def test_the_write_receipt_names_the_scope_and_the_topic():
    """The store guesses both dimensions, so both must be visible in the reply:
    a guess nobody is shown is a guess nobody can correct."""
    config = MemConfig(scope_aware=True)
    conn = connect_for_tests(config)
    try:
        worker = _worker(conn, config)
        worker.domain_store().create("u", "teaching")
        outcome = _write(
            worker, "用户", "偏好", "先讲概念再举例",
            scope_context=PROJECT, domain_hints=["teaching"],
        )
        assert outcome["scope"]["path"].endswith("project:github.com/acme/course")
        assert outcome["domains"][0]["detail"].startswith("primary teaching")
    finally:
        conn.close()
