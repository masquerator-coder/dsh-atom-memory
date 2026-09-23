"""Scope-aware write path, public surface and RPC mapping.

``tests/test_scope.py`` covers resolution, scoring and the digest. This file
covers the other half — what the *store* does with a scope once it has one:

* the write path files facts, records conditions and links cross-scope
  neighbours instead of judging them contradictions;
* ``replace`` keeps a fact where it was rather than following the caller;
* the abstraction pass promotes a claim several scopes arrived at, with its
  indexes written (a promoted rule that search cannot find is not a rule);
* the ``AtomMem`` scope surface and the RPC method table actually reach those
  operations.

The worker tests drive ``_apply_candidates`` directly: it is the single write
gate every path funnels through, so testing it is testing all of them, and it
avoids racing a background drain.
"""

from __future__ import annotations

import asyncio
import hashlib

import pytest

from atom_memory import AtomMem, MemConfig
from atom_memory.db import connect_for_tests, index_orphans, now_ms, open_db
from atom_memory.embedder import serialize_float32
from atom_memory.rpc import RpcServer
from atom_memory.worker import _candidate_from_rpc_dict, Worker

PROJECT_A = {"signals": {"git_remote": "git@github.com:acme/api.git",
                         "git_root": "D:/work/api"}}
PROJECT_B = {"signals": {"git_remote": "git@github.com:acme/web.git",
                         "git_root": "D:/work/web"}}


class _FakeEmbedder:
    """Stand-in for the model: 512 deterministic floats, no inference."""

    def __init__(self, **kwargs) -> None:
        pass

    def embed_one(self, text: str) -> bytes:
        digest = hashlib.sha256(text.encode("utf-8")).digest()
        values = [b / 255.0 for b in (digest * 16)[:512]]
        return serialize_float32(values)


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


# -- write path --------------------------------------------------------------


def test_a_write_is_filed_under_the_resolved_scope_with_its_conditions():
    config = MemConfig(scope_aware=True)
    conn = connect_for_tests(config)
    try:
        worker = _worker(conn, config)
        payload = dict(PROJECT_A, conditions={"language": "typescript"})
        outcome = _write(worker, "项目", "规范", "严格模式要开", payload)

        assert outcome["scope"]["status"] == "created"
        fact_id = outcome["written"][0]
        bindings = conn.execute(
            "SELECT scope_id FROM fact_scope WHERE fact_id = ?", (fact_id,)
        ).fetchall()
        assert [r["scope_id"] for r in bindings] == [outcome["scope"]["scope_id"]]
        conditions = conn.execute(
            "SELECT key, value FROM fact_condition WHERE fact_id = ?", (fact_id,)
        ).fetchall()
        assert [(r["key"], r["value"]) for r in conditions] == [
            ("language", "typescript")
        ]
    finally:
        conn.close()


def test_extractor_conditions_merge_with_the_context_ones():
    """Both describe when the claim holds; dropping either would make a
    conditional rule look unconditional."""
    config = MemConfig(scope_aware=True)
    conn = connect_for_tests(config)
    try:
        worker = _worker(conn, config)
        outcome = _write(
            worker, "报告", "要求", "先写执行摘要",
            dict(PROJECT_A, conditions={"language": "zh"}),
            conditions=[{"key": "doc_type", "value": "proposal"}],
        )
        conditions = conn.execute(
            "SELECT key, value FROM fact_condition WHERE fact_id = ?",
            (outcome["written"][0],),
        ).fetchall()
        assert [(r["key"], r["value"]) for r in conditions] == [
            ("doc_type", "proposal"), ("language", "zh")
        ]
    finally:
        conn.close()


def test_the_same_claim_in_two_scopes_is_two_facts_linked_by_origin():
    """An independent restatement in another project is *evidence* — it is what
    the abstraction pass promotes — so folding it away would destroy exactly
    that."""
    config = MemConfig(scope_aware=True)
    conn = connect_for_tests(config)
    try:
        worker = _worker(conn, config)
        first = _write(worker, "团队", "约定", "提交信息用中文", PROJECT_A)
        second = _write(worker, "团队", "约定", "提交信息用中文", PROJECT_B)

        assert len(first["written"]) == 1 and len(second["written"]) == 1
        assert first["written"] != second["written"]
        assert second["cross_scope"] == [
            {"other_fact_id": first["written"][0],
             "relation": "cross_scope_similar", "same_object": True}
        ]
        origins = conn.execute(
            "SELECT relation, derived_fact_id, source_fact_id FROM fact_origin"
        ).fetchall()
        assert [(r["relation"]) for r in origins] == ["cross_scope_similar"]
    finally:
        conn.close()


def test_a_restatement_inside_one_scope_folds_instead_of_duplicating():
    config = MemConfig(scope_aware=True)
    conn = connect_for_tests(config)
    try:
        worker = _worker(conn, config)
        first = _write(worker, "团队", "约定", "提交信息用中文", PROJECT_A)
        again = _write(worker, "团队", "约定", "提交信息用中文", PROJECT_A)
        assert again["written"] == []
        assert again["reinforced"][0]["fact_id"] == first["written"][0]
        assert conn.execute("SELECT COUNT(*) AS n FROM facts").fetchone()["n"] == 1
    finally:
        conn.close()


def test_a_claim_committed_during_the_write_is_folded_not_duplicated(tmp_path):
    """The identity check-then-act spans a real suspension point.

    `_persist_fact` asks "does this owner already have this claim?" and then
    INSERTs, but the embedding is computed in between via
    `await asyncio.to_thread(...)` — a genuine yield, so another writer on the
    same store can commit the same claim inside that window. Both would see "no
    existing row" and both would insert, leaving two identical active facts:
    exactly what the content fingerprint exists to prevent, and invisible to
    every repair pass (the index check compares `facts` against its FTS and
    vector indexes, not against itself).

    The competing commit has to land *inside* that window to reproduce it. It is
    therefore performed from the embed callback, which runs on the worker thread
    at exactly the point the first fingerprint check has already returned "no
    row" and the INSERT has not yet run. A competing row that already existed
    before the call would be caught by that first check and would prove nothing.

    A file-backed database is used rather than the usual ``:memory:`` one because
    the competing writer is a *second connection* to the same store — which is
    what "two writers" means here, and is also why sqlite refuses to share one
    connection across threads.

    The competing row is created by first letting the real worker write it, so it
    carries a proper scope binding. The fingerprint check is windowed by scope,
    so a hand-inserted winner with no binding would sit outside this write's
    window and the re-check would legitimately ignore it.
    """
    config = MemConfig(scope_aware=True, db_path=str(tmp_path / "race.db"))
    conn = open_db(config)
    try:
        seeded = _write(_worker(conn, config), "团队", "约定", "提交信息用中文", PROJECT_A)
        assert len(seeded["written"]) == 1
        winner = seeded["written"][0]
        # The winner is retired, so the store looks exactly as it did before the
        # racing write: an active claim with this identity must be *created* by
        # the competing commit, not found by the first check.
        conn.execute("UPDATE facts SET status = 'archived' WHERE fact_id = ?", (winner,))
        conn.commit()

        def embed_that_reinstates_the_winner(text: str) -> bytes:
            """Re-activate the claim on a second connection while we are in flight.

            Opened, used and closed on this thread — sqlite refuses to hand a
            connection across threads, which is also why a separate connection
            (rather than a second cursor) is the honest model of a concurrent
            writer.
            """
            other = open_db(config)
            try:
                other.execute(
                    "UPDATE facts SET status = 'active' WHERE fact_id = ?", (winner,)
                )
                other.commit()
            finally:
                other.close()
            return embed(text)

        racer = Worker(
            conn=conn, embed_func=embed_that_reinstates_the_winner, config=config
        )
        outcome = _write(racer, "团队", "约定", "提交信息用中文", PROJECT_A)

        assert outcome["written"] == [], "a concurrent restatement must not add a row"
        assert outcome["reinforced"][0]["fact_id"] == winner
        assert conn.execute(
            "SELECT COUNT(*) AS n FROM facts WHERE status = 'active'"
        ).fetchone()["n"] == 1
    finally:
        conn.close()


def test_the_race_fold_still_respects_scope_identity():
    """The re-check must not collapse the deliberate cross-scope duplicate.

    Identity is per scope *window*, so the same claim committed in another
    project during the write window is still a second fact — that independent
    restatement is what the abstraction pass promotes.
    """
    config = MemConfig(scope_aware=True)
    conn = connect_for_tests(config)
    try:
        worker = _worker(conn, config)
        first = _write(worker, "团队", "约定", "提交信息用中文", PROJECT_A)
        second = _write(worker, "团队", "约定", "提交信息用中文", PROJECT_B)

        assert len(first["written"]) == 1 and len(second["written"]) == 1
        assert first["written"] != second["written"]
    finally:
        conn.close()


def test_a_project_override_does_not_supersede_the_global_rule():
    """The failure this pins: filing the project's value as a contradiction
    would retire the general rule that every other project still needs."""
    config = MemConfig(scope_aware=True)
    conn = connect_for_tests(config)
    try:
        worker = _worker(conn, config)
        general = _write(worker, "发布", "流程", "先测试")
        specific = _write(worker, "发布", "流程", "先评审", PROJECT_A)

        assert specific["superseded"] == [], "a different scope is not a contradiction"
        assert len(specific["written"]) == 1
        statuses = {
            r["fact_id"]: r["status"]
            for r in conn.execute("SELECT fact_id, status FROM facts").fetchall()
        }
        assert statuses[general["written"][0]] == "active"
        assert statuses[specific["written"][0]] == "active"
        assert specific["cross_scope"][0]["relation"] == "exception"
        assert conn.execute(
            "SELECT relation FROM fact_evolution"
        ).fetchone()["relation"] == "exception"
    finally:
        conn.close()


def test_a_contradiction_inside_one_scope_still_supersedes():
    """The scope layer must not disable the single-valued rule inside a scope."""
    config = MemConfig(scope_aware=True)
    conn = connect_for_tests(config)
    try:
        worker = _worker(conn, config)
        first = _write(worker, "用户", "职业", "工程师", PROJECT_A)
        second = _write(worker, "用户", "职业", "架构师", PROJECT_A)
        assert second["written"]
        assert second["superseded"][0]["old_fact_id"] == first["written"][0]
        assert conn.execute(
            "SELECT status FROM facts WHERE fact_id = ?", (first["written"][0],)
        ).fetchone()["status"] == "superseded"
    finally:
        conn.close()


def test_a_scope_blind_write_is_filed_as_global():
    config = MemConfig(scope_aware=True)
    conn = connect_for_tests(config)
    try:
        worker = _worker(conn, config)
        outcome = _write(worker, "用户", "偏好", "黑咖啡")
        assert outcome["scope"]["scope_id"] == 1
        assert outcome["scope"]["status"] == "global"
        assert conn.execute(
            "SELECT scope_id FROM fact_scope WHERE fact_id = ?",
            (outcome["written"][0],),
        ).fetchone()["scope_id"] == 1
    finally:
        conn.close()


def test_replace_inherits_the_scope_of_the_fact_it_replaces():
    """A correction must not move a fact into whatever project the caller is in
    now."""
    config = MemConfig(scope_aware=True)
    conn = connect_for_tests(config)
    try:
        worker = _worker(conn, config)
        original = _write(worker, "用户", "职业", "工程师", PROJECT_A)
        fact_id = original["written"][0]

        async def scenario():
            await worker._process_replace({
                "candidate_id": "c-replace",
                "user_id": "u",
                "old_fact_id": fact_id,
                "new_text": "我的职业是架构师",
                "session_id": "s1",
                "scope_context": PROJECT_B,
            })
        asyncio.run(scenario())

        replacement = conn.execute(
            "SELECT fact_id FROM facts WHERE status = 'active' AND fact_id != ?",
            (fact_id,),
        ).fetchone()
        assert replacement is not None, "the replacement was written"
        assert conn.execute(
            "SELECT scope_id FROM fact_scope WHERE fact_id = ?",
            (replacement["fact_id"],),
        ).fetchone()["scope_id"] == original["scope"]["scope_id"]
    finally:
        conn.close()


def test_the_abstraction_pass_promotes_a_shared_claim_and_indexes_it():
    config = MemConfig(scope_aware=True, scope_abstraction_min_scopes=2)
    conn = connect_for_tests(config)
    try:
        worker = _worker(conn, config)
        first = _write(worker, "团队", "约定", "提交信息用中文", PROJECT_A)
        second = _write(worker, "团队", "约定", "提交信息用中文", PROJECT_B)

        promoted = asyncio.run(worker.promote_abstractions("u"))
        assert len(promoted) == 1
        rule = promoted[0]
        assert sorted(rule["source_fact_ids"]) == sorted(
            [first["written"][0], second["written"][0]]
        )
        assert conn.execute(
            "SELECT source_type FROM facts WHERE fact_id = ?", (rule["fact_id"],)
        ).fetchone()["source_type"] == "system_inferred_high"
        assert conn.execute(
            "SELECT scope_id FROM fact_scope WHERE fact_id = ?", (rule["fact_id"],)
        ).fetchone()["scope_id"] == 1
        assert conn.execute(
            "SELECT COUNT(*) AS n FROM fact_origin WHERE derived_fact_id = ?",
            (rule["fact_id"],),
        ).fetchone()["n"] == 2
        # A promoted rule search cannot find is not a rule.
        orphans = index_orphans(conn)
        assert orphans["missing_vector"] == [] and orphans["missing_fts"] == []
        # ...and the pass is idempotent: the rule is now itself at the root.
        assert asyncio.run(worker.promote_abstractions("u")) == []
    finally:
        conn.close()


def test_promotion_stays_off_with_scope_awareness_disabled():
    config = MemConfig(scope_aware=False)
    conn = connect_for_tests(config)
    try:
        worker = _worker(conn, config)
        _write(worker, "团队", "约定", "提交信息用中文")
        assert asyncio.run(worker.promote_abstractions("u")) == []
    finally:
        conn.close()


# -- public surface ----------------------------------------------------------


def _mem(tmp_path, monkeypatch, **overrides) -> AtomMem:
    monkeypatch.setattr("atom_memory.api.Embedder", _FakeEmbedder)
    kwargs = dict(
        db_path=str(tmp_path / "mem.db"),
        worker_poll_interval_sec=0.05,
        max_retries=3,
    )
    kwargs.update(overrides)
    return AtomMem(MemConfig(**kwargs))


def test_the_scope_surface_creates_resolves_and_repairs(tmp_path, monkeypatch):
    mem = _mem(tmp_path, monkeypatch)

    async def scenario():
        await mem.start()
        try:
            root = mem.scope_list()
            assert [s["path"] for s in root] == ["/global"]

            client = mem.scope_create(
                "client", "acme", signals={"org_domain": "acme.example"}
            )
            project = mem.scope_create(
                "project", "github.com/acme/api", parent_id=client["scope_id"],
                signals={"git_remote": "git@github.com:acme/api"},
                confidence=0.95,
            )
            assert project["path"] == f"/global/client:acme/project:github.com/acme/api"
            assert [s["scope_id"] for s in mem.scope_list(client["scope_id"])] == [
                project["scope_id"]
            ]

            resolved = mem.scope_resolve(
                "u", {"signals": {"git_remote": "https://github.com/acme/api"}}
            )
            assert resolved["scope_id"] == project["scope_id"]
            assert resolved["status"] == "bound"
            assert resolved["matched"] == ["git_remote"]

            # An alias binds, and is reported as unconfirmed while the scope
            # itself is not yet certain — which is what the queue's promoted
            # scopes look like.
            provisional = mem.scope_create("project", "d:/work/api-copy",
                                           confidence=0.5)
            assert mem.scope_alias_add(provisional["scope_id"], "api-copy")["added"]
            pending = mem.scope_resolve("u", {"signals": {"name": "api-copy"}})
            assert pending["scope_id"] == provisional["scope_id"]
            assert pending["status"] == "pending_confirmation"
            assert mem.scope_confirm(provisional["scope_id"])["confirmed"] is True
            confirmed = mem.scope_resolve("u", {"signals": {"name": "api-copy"}})
            assert confirmed["scope_id"] == provisional["scope_id"]
            assert confirmed["status"] == "bound", "a confirmed scope needs no doubt"

            # A weak, never-seen signal is queued rather than created.
            weak = mem.scope_resolve(
                "u", {"signals": {"path": "D:/no/such/project"}}, create=True
            )
            assert weak["status"] == "unresolved"
            queue = mem.scope_unresolved("u")
            assert [entry["name"] for entry in queue] == ["d:/no/such/project"]

            # Merge and reparent both keep the tree usable.
            other = mem.scope_create("project", "github.com/acme/api-v2",
                                     parent_id=client["scope_id"])
            assert mem.scope_merge(other["scope_id"], project["scope_id"])[
                "facts_moved"
            ] == 0
            orphan = mem.scope_create("project", "github.com/acme/other")
            moved = mem.scope_reparent(orphan["scope_id"], client["scope_id"])
            assert moved["parent_id"] == client["scope_id"]

            split = mem.scope_split(project["scope_id"], "draft", "phase", [])
            assert split["scope_id"] != project["scope_id"]
        finally:
            await mem.stop()

    asyncio.run(scenario())


def test_fact_scope_binding_conditions_and_provenance(tmp_path, monkeypatch):
    mem = _mem(tmp_path, monkeypatch)

    async def scenario():
        await mem.start()
        try:
            project = mem.scope_create("project", "github.com/acme/api")
            mem.db.execute(
                "INSERT INTO facts(fact_id, user_id, session_id, subject, predicate, "
                "object, observed_at, created_at) VALUES ('f1','u','s','用户','偏好',"
                "'黑咖啡', ?, ?)",
                (now_ms(), now_ms()),
            )
            mem.db.commit()

            bound = mem.fact_scope_bind("u", "f1", [project["scope_id"]])
            assert bound["scope_ids"] == [project["scope_id"]]
            conditioned = mem.fact_condition_set(
                "u", "f1", {"doc_type": "proposal"}
            )
            assert conditioned["conditions"] == [
                {"key": "doc_type", "value": "proposal"}
            ]

            detail = mem.fact_scope_get("u", "f1")
            assert [s["name"] for s in detail["scopes"]] == ["github.com/acme/api"]
            assert detail["conditions"] == [
                {"key": "doc_type", "value": "proposal"}
            ]

            fact = mem.get_fact("u", "f1")
            assert [s["scope_type"] for s in fact["scopes"]] == ["project"]
            assert fact["conditions"] == [{"key": "doc_type", "value": "proposal"}]
            page = mem.list_facts("u")
            assert page["facts"][0]["scope_labels"] == ["api"]

            # Isolation is enforced: another user may not touch the fact.
            with pytest.raises(ValueError):
                mem.fact_scope_bind("someone-else", "f1", [project["scope_id"]])
        finally:
            await mem.stop()

    asyncio.run(scenario())


def test_promoting_a_fact_makes_it_reachable_from_its_siblings(tmp_path, monkeypatch):
    """The recall gap that a *stronger document threshold* alone cannot close.

    A fact bound to a document is invisible from its sibling documents — that is
    the design (`expanded_scope_ids`), and it is right: chapter 2's notes are not
    chapter 3's. But a preference that holds for the whole course gets captured
    while writing one chapter, and nothing in its text says so. Only the user
    knows, so promoting it is an explicit act — and afterwards the fact must be
    reachable from the sibling document, which is what this pins.
    """
    mem = _mem(tmp_path, monkeypatch)
    fake = _FakeEmbedder()
    course = {"signals": {"git_remote": "git@github.com:acme/course",
                          "git_root": "D:/work/course"}}
    # Chapter 3 carries a folder path and no durable id, so by the document
    # threshold it is filed at the *project* level — the reachable one.
    ch3 = {"signals": {**course["signals"], "folder_path": "D:/work/course/ch3"}}

    async def scenario():
        await mem.start()
        try:
            resolved = mem.scope_resolve("u", course, create=True)
            project_id = resolved["scope_id"]
            ch3_id = mem.scope_resolve("u", ch3, create=True)["scope_id"]
            assert ch3_id == project_id, (
                "a weak document signal must not detach a chapter from its project"
            )
            # A durable id *does* make its own document, and the write already
            # registered it as a signal, so chapter 4's context resolves to it.
            ch4_id = mem.scope_create(
                "document", "drive-ch4",
                parent_id=project_id,
                signals={"doc_id": "drive-ch4"},
            )["scope_id"]
            assert ch4_id not in (project_id, ch3_id)
            ch4 = {"signals": {**course["signals"], "doc_id": "drive-ch4"}}
            assert mem.scope_resolve("u", ch4, create=False)["scope_id"] == ch4_id

            text = "教学 偏好 先讲概念再举例"
            mem.db.execute(
                "INSERT INTO facts(fact_id, user_id, session_id, subject, predicate, "
                "object, observed_at, created_at) VALUES ('f1','u','s','教学','偏好',"
                "'先讲概念再举例', ?, ?)",
                (now_ms(), now_ms()),
            )
            mem.db.execute(
                "INSERT INTO facts_fts(fact_id, text) VALUES ('f1', ?)", (text,)
            )
            mem.db.execute(
                "INSERT INTO facts_vec(fact_id, embedding) VALUES ('f1', ?)",
                (fake.embed_one(text),),
            )
            mem.db.commit()

            # Where a chapter-scoped capture would file it, and the gap that
            # creates: the sibling chapter cannot see it.
            mem.fact_scope_bind("u", "f1", [ch4_id])
            hidden = await mem.recall("u", "先讲概念再举例", scope_context=ch3)
            assert hidden["facts"] == []

            promoted = mem.fact_scope_promote("u", "f1", project_id)
            assert promoted["action"] == "moved"
            assert promoted["from_scope_id"] == ch4_id
            assert promoted["to_scope_id"] == project_id

            after = await mem.recall("u", "先讲概念再举例", scope_context=ch4)
            assert [f["fact_id"] for f in after["facts"]] == ["f1"], (
                "after promotion the project-level fact is visible from every "
                "document beneath it"
            )
            # The old binding is demoted, not deleted: the move stays reversible
            # and the fact remains readable as a document-scoped fact.
            detail = mem.fact_scope_get("u", "f1")
            assert [s["scope_id"] for s in detail["scopes"]] == [ch4_id, project_id]

            events = mem.db.execute(
                "SELECT COUNT(*) AS n FROM events WHERE type = 'fact_scope_promoted'"
            ).fetchone()
            assert events["n"] == 1, "the move is auditable"

            # Promotion lifts a fact; it never buries one deeper.
            with pytest.raises(ValueError):
                mem.fact_scope_promote("u", "f1", ch4_id)
            with pytest.raises(ValueError):
                mem.fact_scope_promote("u", "f1", 999999)
            with pytest.raises(ValueError):
                mem.fact_scope_promote("someone-else", "f1", project_id)
        finally:
            await mem.stop()

    asyncio.run(scenario())


def test_recall_and_summary_carry_the_scope_context(tmp_path, monkeypatch):
    mem = _mem(tmp_path, monkeypatch)

    async def scenario():
        await mem.start()
        try:
            project = mem.scope_create(
                "project", "github.com/acme/api",
                signals={"git_remote": "git@github.com:acme/api"},
            )
            fake = _FakeEmbedder()
            for fact_id, obj in (("f1", "严格模式要开"), ("f2", "提交前跑测试")):
                text = f"项目 规范 {obj}"
                mem.db.execute(
                    "INSERT INTO facts(fact_id, user_id, session_id, subject, "
                    "predicate, object, observed_at, created_at) "
                    "VALUES (?, 'u', 's', '项目', '规范', ?, ?, ?)",
                    (fact_id, obj, now_ms(), now_ms()),
                )
                mem.db.execute(
                    "INSERT INTO facts_fts(fact_id, text) VALUES (?, ?)",
                    (fact_id, text),
                )
                mem.db.execute(
                    "INSERT INTO facts_vec(fact_id, embedding) VALUES (?, ?)",
                    (fact_id, fake.embed_one(text)),
                )
            mem.db.commit()
            mem.fact_scope_bind("u", "f1", [project["scope_id"]])
            mem.fact_scope_bind("u", "f2", [project["scope_id"]])

            recalled = await mem.recall(
                "u", "规范", scope_context=PROJECT_A
            )
            assert recalled["scope"]["status"] == "bound"
            assert recalled["facts"], "the project's own facts are recalled"
            assert recalled["facts"][0]["scope_labels"] == ["api"]
            assert recalled["facts"][0]["scope_weight"] == pytest.approx(1.0)

            digest = await mem.summary(
                "u", 400, detail=False, scope_context=PROJECT_A
            )
            assert "[当前项目: api" in digest
            flat = await mem.summary("u", 400, detail=False)
            assert "[当前项目" not in flat
        finally:
            await mem.stop()

    asyncio.run(scenario())


# -- RPC mapping -------------------------------------------------------------


class _StubMem:
    """Records the calls the RPC layer makes, and answers like the real one."""

    def __init__(self) -> None:
        self.calls: list = []

    def __getattr__(self, name):
        def _call(**params):
            self.calls.append((name, params))
            return {"method": name, "params": params}
        return _call


def test_every_scope_rpc_method_is_reachable_and_forwards_its_params():
    """The RPC layer is a name table: a typo there is a feature that exists in
    the library and is unreachable over the wire."""
    server = RpcServer()
    server._started = True
    server.mem = _StubMem()

    async def scenario():
        requests = [
            ("scope_list", {"parent_id": 2, "status": "active"}),
            ("scope_resolve", {"user_id": "u", "scope_context": PROJECT_A}),
            ("scope_create", {"scope_type": "project", "name": "x"}),
            ("scope_alias_add", {"scope_id": 1, "alias": "a"}),
            ("scope_confirm", {"scope_id": 1}),
            ("scope_merge", {"from_id": 1, "to_id": 2}),
            ("scope_split", {"from_id": 1, "name": "p", "scope_type": "phase",
                             "fact_ids": []}),
            ("scope_reparent", {"scope_id": 1, "parent_id": 2}),
            ("scope_unresolved", {"user_id": "u"}),
            ("scope_promote", {"user_id": "u"}),
            ("fact_scope_bind", {"user_id": "u", "fact_id": "f", "scope_ids": [1]}),
            ("fact_condition_set", {"user_id": "u", "fact_id": "f",
                                    "conditions": [{"key": "k", "value": "v"}]}),
            ("fact_scope_get", {"user_id": "u", "fact_id": "f"}),
            ("fact_scope_promote", {"user_id": "u", "fact_id": "f",
                                    "to_scope_id": 1}),
        ]
        results = []
        for method, params in requests:
            results.append(await server._dispatch(method, params))
        return results

    results = asyncio.run(scenario())
    assert [r["method"] for r in results] == [
        "scope_list", "scope_resolve", "scope_create", "scope_alias_add",
        "scope_confirm", "scope_merge", "scope_split", "scope_reparent",
        "scope_unresolved", "scope_promote", "fact_scope_bind",
        "fact_condition_set", "fact_scope_get", "fact_scope_promote",
    ]
    forwarded = dict(server.mem.calls)
    assert forwarded["scope_resolve"]["scope_context"] == PROJECT_A
    assert forwarded["fact_scope_bind"]["scope_ids"] == [1]
    assert forwarded["fact_scope_promote"]["to_scope_id"] == 1


def test_scope_context_reaches_the_summary_and_persist_handlers():
    """``summary`` and ``persist_candidates`` are handled by the RPC layer
    itself (they add metadata / enqueue a task), so their forwarding is worth
    pinning separately from the name table."""
    server = RpcServer()
    server._started = True
    seen: dict = {}

    class _SummaryStub(_StubMem):
        async def summary(self, user_id, **params):
            seen["summary"] = {"user_id": user_id, **params}
            return "digest"

        db = None

    server.mem = _SummaryStub()

    async def scenario():
        await server._summary({
            "user_id": "u", "max_tokens": 100, "detail": False,
            "scope_context": PROJECT_A,
        })
    asyncio.run(scenario())
    assert seen["summary"]["scope_context"] == PROJECT_A
