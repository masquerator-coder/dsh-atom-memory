"""Tests for scope awareness: context signals, resolution, hierarchy, scoring.

The scope dimension is the answer to four failure modes that are otherwise
invisible, so each test here names the one it pins:

* **cross-project pollution** — project B's rule must not be recalled for
  project A (`test_recall_excludes_a_sibling_project...`);
* **a global rule that cannot be told apart from a project's** — both must come
  back, the project's first (`test_recall_ranks_the_project_rule_above_the_global_one`);
* **a project's override destroying the general rule** — the two must coexist
  and be linked, never supersede each other
  (`test_a_project_override_does_not_supersede_the_global_rule`);
* **the same claim recorded twice** — an independent restatement in another
  scope is evidence, not a duplicate
  (`test_the_same_claim_in_two_scopes_is_two_facts`).

Everything is exercised against a real in-memory database: the resolution rules
are *about* SQL uniqueness (one signal identifies one scope), so a fake store
would test nothing.
"""

from __future__ import annotations

import asyncio
import hashlib
import re
import uuid

import pytest

from atom_memory.config import MemConfig
from atom_memory.context import (
    GLOBAL_SCOPE_ID,
    ChainLevel,
    context_from_payload,
    make_signal,
    normalize_conditions,
    normalize_path,
    normalize_remote,
)
from atom_memory.db import connect_for_tests, now_ms
from atom_memory.retriever import Retriever
from atom_memory.scope import (
    STATUS_BOUND,
    STATUS_CREATED,
    STATUS_GLOBAL,
    STATUS_PENDING,
    STATUS_UNRESOLVED,
    ScopeStore,
    condition_match,
    distance_weight,
    expanded_scope_ids,
    resolution_for,
)
from atom_memory.summary import generate_summary
from atom_memory.validator import cross_scope_neighbours, validate
from atom_memory.models import FactCandidate

PROJECT_A = {"signals": {"git_remote": "git@github.com:acme/api.git",
                         "git_root": "D:/work/api"}}
PROJECT_B = {"signals": {"git_remote": "git@github.com:acme/web.git",
                         "git_root": "D:/work/web"}}


# -- fixtures ----------------------------------------------------------------


def embed(text: str) -> bytes:
    """Deterministic pseudo-embedding (512 float32), stable across runs.

    Retrieval here is about *which* facts are candidates and how they are
    weighted, never about semantic closeness, so an exact-but-arbitrary vector is
    the honest stub: it keeps the KNN path exercised without pretending to rank.
    """
    digest = hashlib.sha256(text.encode("utf-8")).digest()
    return (digest * 64)[: 512 * 4]


@pytest.fixture()
def config():
    return MemConfig(scope_aware=True)


@pytest.fixture()
def conn(config):
    connection = connect_for_tests(config)
    yield connection
    connection.close()


def add_fact(conn, scope_ids, subject="项目", predicate="规范", obj="严格模式",
             conditions=(), memory_type="semantic", user="u", importance=0.9):
    """Insert an active fact directly, bound to ``scope_ids``.

    The content fingerprint is derived from the SPO text (as
    :mod:`atom_memory.fingerprint` derives it in production), so two rows with
    the same claim share one identity and the cross-scope promotion pass can see
    them as the same claim.
    """
    fact_id = str(uuid.uuid4())
    ts = now_ms()
    with conn:
        conn.execute(
            "INSERT INTO facts(fact_id, user_id, session_id, subject, predicate, "
            "object, type, importance, confidence, observed_at, created_at, "
            "content_fingerprint) VALUES (?, ?, 's', ?, ?, ?, ?, ?, 0.9, ?, ?, ?)",
            (fact_id, user, subject, predicate, obj, memory_type, importance,
             ts, ts, f"fp:{subject}|{predicate}|{obj}"),
        )
        conn.execute(
            "INSERT INTO facts_fts(fact_id, text) VALUES (?, ?)",
            (fact_id, f"{subject} {predicate} {obj}"),
        )
        conn.execute(
            "INSERT INTO facts_vec(fact_id, embedding) VALUES (?, ?)",
            (fact_id, embed(f"{subject} {predicate} {obj}")),
        )
        for scope_id in scope_ids:
            conn.execute(
                "INSERT INTO fact_scope(fact_id, scope_id, priority) VALUES (?, ?, 0)",
                (fact_id, scope_id),
            )
        for key, value in conditions:
            conn.execute(
                "INSERT INTO fact_condition(fact_id, key, value) VALUES (?, ?, ?)",
                (fact_id, key, value),
            )
    return fact_id


# -- signal normalisation ----------------------------------------------------


@pytest.mark.parametrize(
    "raw,expected",
    [
        ("git@GitHub.com:Team/Repo.git", "github.com/team/repo"),
        ("https://user:secret@github.com/team/repo/", "github.com/team/repo"),
        ("ssh://git@github.com:22/team/repo.git", "github.com/team/repo"),
        ("", ""),
    ],
)
def test_remotes_normalise_to_one_identity(raw, expected):
    """Three spellings of one remote are one project, and credentials never
    survive normalisation (a token in a signal value would be stored)."""
    assert normalize_remote(raw) == expected


def test_paths_normalise_case_and_separators():
    assert normalize_path("D:\\Coding\\..\\Coding\\Repo\\") == "d:/coding/repo"
    assert normalize_path("~/x/y") == normalize_path("~/x/y")


def test_a_remote_credential_never_reaches_the_store(conn, config):
    """A signal keeps its raw value for the audit trail, so a token in the URL
    would sit in the database (and in every backup) for no benefit: the identity
    is computed from the normalised value, from which both spellings are gone."""
    from atom_memory.context import safe_raw_value

    assert safe_raw_value(
        "git_remote", "https://user:tok3n@github.com/acme/api.git"
    ) == "https://github.com/acme/api.git"
    assert safe_raw_value(
        "git_remote", "https://github.com/acme/api.git?access_token=tok3n#frag"
    ) == "https://github.com/acme/api.git"
    # A scp-style remote carries a user *name*, not a credential.
    assert safe_raw_value(
        "git_remote", "git@github.com:acme/api.git"
    ) == "git@github.com:acme/api.git"

    resolution_for(
        conn,
        config,
        {"signals": {"git_remote": "https://user:tok3n@github.com/acme/api.git"}},
        user_id="u",
    )
    stored = conn.execute("SELECT signal_value FROM scope_signal").fetchall()
    assert [r["signal_value"] for r in stored] == ["https://github.com/acme/api.git"]
    assert all("tok3n" not in r["signal_value"] for r in stored)


def test_conditions_are_key_shaped_and_lowercased():
    pairs = normalize_conditions([
        {"key": "Doc_Type", "value": " Proposal "},
        {"key": "not a key", "value": "x"},
        {"key": "language", "value": ""},
        ("language", "typescript"),
    ])
    assert pairs == (("doc_type", "proposal"), ("language", "typescript"))


def test_a_context_with_nothing_usable_is_none():
    """``None`` is what keeps a scope-blind caller on the pre-scope pipeline,
    so an empty payload must not manufacture an empty context."""
    assert context_from_payload({}, user_id="u") is None
    assert context_from_payload({"signals": {}}, user_id="u") is None
    assert context_from_payload({"scope_hint": "global"}, user_id="u") is None
    assert context_from_payload({"scope_hint": "user"}, user_id="u") is None


def test_a_level_marker_is_never_turned_into_a_scope_name(conn, config):
    """``scope_hint`` is normally a name the extractor copied out of the text, and
    as 0.25 evidence it accumulates in the candidate queue. The two *level*
    markers are different in kind — they say "this belongs to the user", not
    "this belongs to a place called user" — so they must be dropped rather than
    queued, or a deployment that prefers durable facts at the user level would
    collect one bogus project per session."""
    for marker in ("user", "User", " global "):
        resolution_for(conn, config, {"scope_hint": marker}, user_id="u")
    assert conn.execute(
        "SELECT COUNT(*) AS n FROM scope"
    ).fetchone()["n"] == 1, "only the root should exist"
    assert conn.execute(
        "SELECT COUNT(*) AS n FROM scope_candidate"
    ).fetchone()["n"] == 0, "a level marker is not a hypothesis about a place"

    _, real = resolution_for(
        conn, config, {"scope_hint": "第3章课件"}, user_id="u"
    )
    assert real.status == STATUS_UNRESOLVED
    assert [c.canonical_name for c in real.candidates] == ["第3章课件"]


# -- resolution --------------------------------------------------------------


def test_a_remote_creates_a_project_scope(conn, config):
    _, resolution = resolution_for(conn, config, PROJECT_A, user_id="u")
    assert resolution.status == STATUS_CREATED
    assert resolution.scope_id != GLOBAL_SCOPE_ID
    assert conn.execute(
        "SELECT scope_type FROM scope WHERE id = ?", (resolution.scope_id,)
    ).fetchone()["scope_type"] == "project"


def test_the_same_repo_cloned_elsewhere_resolves_to_the_same_scope(conn, config):
    """Identity is the *signal*, not the local path: a clone at a different
    path is the same project."""
    _, first = resolution_for(conn, config, PROJECT_A, user_id="u")
    _, second = resolution_for(
        conn,
        config,
        {"signals": {"git_remote": "https://github.com/acme/api", "path": "C:/other"}},
        user_id="u",
    )
    assert second.status == STATUS_BOUND
    assert second.scope_id == first.scope_id


def test_a_low_confidence_signal_is_queued_not_created(conn, config):
    """A folder path alone is 中 (0.50) evidence: below the creation threshold
    it must not create a scope, but the store must remember it kept coming up."""
    for expected_count in (1, 2):
        _, resolution = resolution_for(
            conn, config, {"signals": {"path": "D:/work/plain"}}, user_id="u"
        )
        assert resolution.status == STATUS_UNRESOLVED
        assert resolution.scope_id == GLOBAL_SCOPE_ID
        assert resolution.candidates[0].seen_count == expected_count
    assert conn.execute(
        "SELECT COUNT(*) AS n FROM scope WHERE scope_type = 'project'"
    ).fetchone()["n"] == 0


def test_a_third_consistent_sighting_promotes_the_candidate(conn, config):
    payload = {"signals": {"path": "D:/work/plain"}}
    for _ in range(config.scope_promote_after - 1):
        resolution_for(conn, config, payload, user_id="u")
    _, resolution = resolution_for(conn, config, payload, user_id="u")
    assert resolution.status == STATUS_CREATED
    assert conn.execute(
        "SELECT scope_type FROM scope WHERE id = ?", (resolution.scope_id,)
    ).fetchone()["scope_type"] == "project"


def test_a_weak_signal_may_not_create_a_document(conn, config):
    """A document is the one scope type whose facts are unreachable from their
    siblings, so the weak signals that merely *name* a document (a folder path,
    a title — both 0.50) must not bring one into existence. Doing so buries the
    fact: the notes taken while writing chapter 3 could never be recalled while
    working on chapter 4, which is exactly the recall gap this threshold closes."""
    _, resolution = resolution_for(
        conn,
        config,
        {"signals": {"git_remote": "git@github.com:acme/course.git",
                     "folder_path": "D:/work/course/ch3",
                     "doc_title": "第3章课件"}},
        user_id="u",
    )
    assert resolution.status == STATUS_CREATED
    assert conn.execute(
        "SELECT scope_type FROM scope WHERE id = ?", (resolution.scope_id,)
    ).fetchone()["scope_type"] == "project"
    assert conn.execute(
        "SELECT COUNT(*) AS n FROM scope WHERE scope_type = 'document'"
    ).fetchone()["n"] == 0


def test_a_durable_document_id_still_creates_a_document(conn, config):
    """The gate is on *evidence*, not on the type: a durable id (0.90) is enough.
    Suppressing document creation outright would lose the real ones."""
    _, resolution = resolution_for(
        conn,
        config,
        {"signals": {"git_remote": "git@github.com:acme/course.git",
                     "doc_id": "drive-file-42"}},
        user_id="u",
    )
    assert resolution.status == STATUS_CREATED
    assert conn.execute(
        "SELECT scope_type FROM scope WHERE id = ?", (resolution.scope_id,)
    ).fetchone()["scope_type"] == "document"


def test_a_weak_document_signal_binds_to_a_document_that_already_exists(conn, config):
    """The threshold governs *creation*. A path or a title seen again still binds:
    re-identification is proof of the identity, and refusing it would split one
    document's history across two scopes."""
    _, created = resolution_for(
        conn,
        config,
        {"signals": {"git_remote": "git@github.com:acme/course.git",
                     "doc_id": "drive-file-42"}},
        user_id="u",
    )
    _, bound = resolution_for(
        conn,
        config,
        {"signals": {"git_remote": "git@github.com:acme/course.git",
                     "doc_id": "drive-file-42",
                     "folder_path": "D:/work/course/ch3"}},
        user_id="u",
    )
    assert bound.status == STATUS_BOUND
    assert bound.scope_id == created.scope_id


def test_the_document_threshold_is_configurable_and_general_levels_are_unaffected(
    conn,
):
    """A deployment that wants the old behaviour sets the threshold back down, and
    the general threshold never moves onto other scope types (a project created
    from a bare path is still a project — and it is reachable from its phases and
    documents, which is why only ``document`` / ``thread`` are gated)."""
    permissive = MemConfig(scope_aware=True, scope_new_threshold_document=0.5)
    _, resolution = resolution_for(
        conn, permissive, {"signals": {"folder_path": "D:/work/course/ch3"}},
        user_id="u",
    )
    assert conn.execute(
        "SELECT scope_type FROM scope WHERE id = ?", (resolution.scope_id,)
    ).fetchone()["scope_type"] == "document"

    _, project = resolution_for(
        conn, permissive, {"signals": {"path": "D:/work/plain"}}, user_id="u"
    )
    assert project.scope_id == GLOBAL_SCOPE_ID, "0.50 still cannot create a project"


def test_a_project_with_a_weak_document_below_it_is_still_created(conn, config):
    """The walk stops at the level it cannot create instead of skipping it: a
    document filed under the *root* would be visible from every unrelated
    context, which is worse than filing the fact at the project."""
    _, resolution = resolution_for(
        conn,
        config,
        {"signals": {"git_remote": "git@github.com:acme/course.git",
                     "folder_path": "D:/work/course/ch3"}},
        user_id="u",
    )
    assert resolution.status == STATUS_CREATED
    row = conn.execute(
        "SELECT scope_type, parent_id FROM scope WHERE id = ?",
        (resolution.scope_id,),
    ).fetchone()
    assert row["scope_type"] == "project"
    assert row["parent_id"] == GLOBAL_SCOPE_ID



def test_a_second_different_signal_does_not_promote_the_first(conn, config):
    """Promotion requires *consistent* sightings: two different folder paths
    under the same name are two pieces of evidence, not two confirmations of
    one — a shared counter would have promoted after three of them."""
    for _ in range(2):
        resolution_for(
            conn, config, {"signals": {"path": "D:/a/plain"}}, user_id="u"
        )
        resolution_for(
            conn, config, {"signals": {"path": "D:/b/plain"}}, user_id="u"
        )
    rows = conn.execute(
        "SELECT normalized_value, seen_count, status FROM scope_candidate "
        "ORDER BY normalized_value"
    ).fetchall()
    assert [(r["normalized_value"], r["seen_count"], r["status"]) for r in rows] == [
        ("d:/a/plain", 2, "pending"),
        ("d:/b/plain", 2, "pending"),
    ]
    assert conn.execute(
        "SELECT COUNT(*) AS n FROM scope WHERE scope_type = 'project'"
    ).fetchone()["n"] == 0


def test_promotion_retires_only_the_candidate_that_matured(conn, config):
    """A sibling candidate under the same name must stay promotable.

    The queue's unique key is (user, type, name, signal_type, normalized_value),
    so two different signals spelling the same name are two independent rows
    with independent counters. The retirement UPDATE matched on the user, type
    and name alone, which marked every sibling `promoted` — and since promotion
    is only ever offered to `pending` rows, those siblings could never be
    created afterwards, no matter how often they were seen.

    Built at the store level because that is where the two rows coexist: the
    resolver's own chain walks one signal per level, so a payload-level test
    cannot put two candidates under one name.
    """
    store = ScopeStore(conn, config)
    now = now_ms()
    for signal_type in ("path", "folder_path"):
        conn.execute(
            "INSERT INTO scope_candidate(user_id, scope_type, canonical_name, "
            "parent_id, signal_type, signal_value, normalized_value, confidence, "
            "seen_count, status, first_seen, last_seen) "
            "VALUES ('u', 'project', 'proj', ?, ?, 'D:/work/proj', 'd:/work/proj', "
            "0.9, ?, 'pending', ?, ?)",
            (GLOBAL_SCOPE_ID, signal_type, config.scope_promote_after, now, now),
        )
    conn.commit()

    matured = make_signal("path", "D:/work/proj")
    promoted = store._promote_candidate(
        ChainLevel(
            scope_type="project",
            canonical_name="proj",
            signals=(matured,),
            confidence=0.9,
            display_name="proj",
        ),
        "u",
        GLOBAL_SCOPE_ID,
    )
    assert promoted is not None, "the candidate matured into a scope"

    statuses = {
        row["signal_type"]: row["status"]
        for row in conn.execute(
            "SELECT signal_type, status FROM scope_candidate WHERE user_id = 'u'"
        ).fetchall()
    }
    assert statuses["path"] == "promoted", "the matured candidate is retired"
    assert statuses["folder_path"] == "pending", (
        "a sibling the promotion never covered stays promotable"
    )


def test_resolution_is_read_only_for_a_query(conn, config):
    """A recall must never create a scope or extend the queue: the answer would
    otherwise depend on how often it had been asked."""
    before = conn.execute("SELECT COUNT(*) AS n FROM scope").fetchone()["n"]
    _, resolution = resolution_for(
        conn, config, PROJECT_A, user_id="u", create=False
    )
    assert resolution.status == STATUS_GLOBAL
    assert conn.execute("SELECT COUNT(*) AS n FROM scope").fetchone()["n"] == before
    assert conn.execute(
        "SELECT COUNT(*) AS n FROM scope_candidate"
    ).fetchone()["n"] == 0


def test_a_recorded_alias_binds_but_is_marked_unconfirmed(conn, config):
    """An alias is someone's *belief* that two identities are the same, so a
    match on it binds — and says so, because the user may want to confirm it."""
    store = ScopeStore(conn, config)
    project = store.create("project", "github.com/acme/api")
    store.add_alias(project, "api-repo", "name", 0.5)
    _, resolution = resolution_for(
        conn, config, {"signals": {"name": "api-repo"}}, user_id="u"
    )
    assert resolution.scope_id == project
    assert resolution.status == STATUS_PENDING


def test_hierarchy_is_created_from_the_most_general_level_down(conn, config):
    """An explicit client plus a remote produce client → project, so recall
    from the project reaches the client's rules through the ancestor walk."""
    _, resolution = resolution_for(
        conn,
        config,
        {"signals": {"explicit_client": "Acme", "git_remote": "git@github.com:acme/api"}},
        user_id="u",
    )
    project = resolution.scope_id
    assert resolution.status == STATUS_CREATED
    upward, _phases = expanded_scope_ids(conn, project)
    types = [
        conn.execute("SELECT scope_type FROM scope WHERE id = ?", (sid,)).fetchone()[
            "scope_type"
        ]
        for sid in upward
    ]
    assert types == ["project", "client", "global"]


def test_creating_a_second_scope_for_a_known_signal_is_refused(conn, config):
    """A signal is a unique identity. Creating a second scope for it would split
    one project's memory in two — the near-duplicate failure the design warns
    about — so ``create`` refuses and names the owner instead."""
    store = ScopeStore(conn, config)
    from atom_memory.context import make_signal

    first = store.create(
        "project", "github.com/acme/api",
        signals=[make_signal("git_remote", "git@github.com:acme/api")],
    )
    with pytest.raises(ValueError, match="already identifies scope"):
        store.create(
            "project", "somewhere-else",
            signals=[make_signal("git_remote", "https://github.com/acme/api")],
        )
    # The same restriction applies to an alias, which is an identity claim too.
    store.add_alias(first, "api-repo", "name", 0.5)
    with pytest.raises(ValueError, match="already identifies scope"):
        store.create(
            "project", "another",
            signals=[make_signal("name", "api-repo")],
        )


# -- expansion and scoring ---------------------------------------------------


def test_visibility_is_upward_plus_phases_and_nothing_else(conn, config):
    """A document-scoped fact must not be visible from its sibling document;
    only a *phase* descendant is (the design's "默认召回所有阶段的 active 事实")."""
    store = ScopeStore(conn, config)
    client = store.create("client", "acme")
    project = store.create("project", "api", parent_id=client)
    phase = store.create("phase", "draft", parent_id=project)
    doc = store.create("document", "spec", parent_id=project)

    upward, phases = expanded_scope_ids(conn, project)
    assert upward == [project, client, GLOBAL_SCOPE_ID]
    assert phases == [phase]
    assert doc not in upward and doc not in phases


def test_distance_weights_follow_the_design_table(conn, config):
    store = ScopeStore(conn, config)
    client = store.create("client", "acme")
    project = store.create("project", "api", parent_id=client)
    scope_view = store.view(
        type("R", (), {"scope_id": project, "confidence": 1.0, "status": "bound"})()
    )
    assert scope_view.weight_for([project]) == pytest.approx(1.0)
    assert scope_view.weight_for([client]) == pytest.approx(0.8)
    assert scope_view.weight_for([GLOBAL_SCOPE_ID]) == pytest.approx(0.5)
    assert scope_view.weight_for([]) == pytest.approx(0.5), "unbound reads as global"


def test_sibling_scope_weighs_less_than_an_ancestor(conn, config):
    store = ScopeStore(conn, config)
    client = store.create("client", "acme")
    project_a = store.create("project", "api", parent_id=client)
    project_b = store.create("project", "web", parent_id=client)
    scope_view = store.view(
        type("R", (), {"scope_id": project_a, "confidence": 1.0, "status": "bound"})()
    )
    assert scope_view.weight_for([project_b]) == pytest.approx(0.3)
    assert scope_view.weight_for([GLOBAL_SCOPE_ID]) > scope_view.weight_for([project_b])


def test_distance_weight_is_relative_to_the_query_scope():
    ancestors = {2: 1, 1: 2}
    assert distance_weight(3, 3, ancestors) == pytest.approx(1.0)
    assert distance_weight(3, 2, ancestors) == pytest.approx(0.8)
    assert distance_weight(3, 9, {2: 1}) == pytest.approx(0.15), "unrelated scope"
    assert distance_weight(3, GLOBAL_SCOPE_ID, {2: 1}) == pytest.approx(0.5)


def test_condition_match_scores_partial_agreement():
    neutral = condition_match([], {"doc_type": ["proposal"]})
    assert neutral == pytest.approx(0.5), "silence is not a mismatch"
    assert condition_match([("language", "ts")], {}) == pytest.approx(0.5)
    assert condition_match(
        [("doc_type", "proposal")], {"doc_type": ["proposal"]}
    ) == pytest.approx(1.0)
    assert condition_match(
        [("doc_type", "proposal"), ("language", "ts")], {"doc_type": ["proposal"]}
    ) == pytest.approx(0.5)
    assert condition_match(
        [("doc_type", "proposal")], {"doc_type": ["report"]}
    ) == pytest.approx(0.0)


# -- recall ------------------------------------------------------------------


def test_recall_excludes_a_sibling_project_without_a_condition_match(
    conn, config
):
    """Scope-aware recall is an async pipeline; the repo's convention is
    ``asyncio.run`` inside a sync test (no pytest-asyncio dependency)."""

    async def scenario():
        store = ScopeStore(conn, config)
        _, resolution_a = resolution_for(conn, config, PROJECT_A, user_id="u")
        _, resolution_b = resolution_for(conn, config, PROJECT_B, user_id="u")
        add_fact(conn, [resolution_a.scope_id], obj="严格模式要开")
        add_fact(conn, [resolution_b.scope_id], obj="宽松模式即可")

        retriever = Retriever(conn, embed, config=config)
        ranked = await retriever.search("u", "模式", scope_context=PROJECT_A)
        objects = [f["object"] for f in ranked]
        assert "严格模式要开" in objects
        assert "宽松模式即可" not in objects, "another project's rule must not be recalled"

    asyncio.run(scenario())


def test_recall_reaches_a_condition_matching_fact_from_another_scope(conn, config):
    """The design's "条件匹配的其他 scope（降权）": a proposal written for one
    client is legitimately useful evidence for another proposal."""

    async def scenario():
        _, resolution_b = resolution_for(conn, config, PROJECT_B, user_id="u")
        add_fact(conn, [resolution_b.scope_id], subject="报告", predicate="要求",
                 obj="先写执行摘要", conditions=[("doc_type", "proposal")])

        retriever = Retriever(conn, embed, config=config)
        payload = dict(PROJECT_A, conditions={"doc_type": "proposal"})
        ranked = await retriever.search("u", "报告 摘要", scope_context=payload)
        assert [f["object"] for f in ranked] == ["先写执行摘要"]
        assert ranked[0]["scope_weight"] == pytest.approx(0.3), "sibling scope"
        assert ranked[0]["condition_match"] == pytest.approx(1.0)
        assert ranked[0]["scope_labels"] == ["web"]

    asyncio.run(scenario())


def test_recall_ranks_the_project_rule_above_the_global_one(conn, config):
    """Both statements are returned — "具体覆盖一般" keeps the exception visible
    — with the project's own ranked higher by the scope term."""

    async def scenario():
        _, resolution_a = resolution_for(conn, config, PROJECT_A, user_id="u")
        add_fact(conn, [GLOBAL_SCOPE_ID], subject="团队", predicate="约定",
                 obj="注释用中文")
        add_fact(conn, [resolution_a.scope_id], subject="团队", predicate="约定",
                 obj="注释用英文")

        retriever = Retriever(conn, embed, config=config)
        ranked = await retriever.search("u", "注释 约定", scope_context=PROJECT_A)
        objects = [f["object"] for f in ranked]
        assert objects == ["注释用英文", "注释用中文"]
        assert ranked[0]["scope_weight"] == pytest.approx(1.0)
        assert ranked[1]["scope_weight"] == pytest.approx(0.5)

    asyncio.run(scenario())


def test_a_scope_blind_query_keeps_the_pre_scope_pipeline(conn, config):
    """No context means no scope filter *and* no scope annotations: an existing
    caller must get exactly what it got before the dimension existed."""

    async def scenario():
        _, resolution_a = resolution_for(conn, config, PROJECT_A, user_id="u")
        _, resolution_b = resolution_for(conn, config, PROJECT_B, user_id="u")
        add_fact(conn, [resolution_a.scope_id], obj="甲")
        add_fact(conn, [resolution_b.scope_id], obj="乙")

        retriever = Retriever(conn, embed, config=config)
        ranked = await retriever.search("u", "规范 模式")
        assert sorted(f["object"] for f in ranked) == ["乙", "甲"]
        assert all("scope_weight" not in f for f in ranked)
        assert retriever.last_scope is None

    asyncio.run(scenario())


def test_scope_awareness_can_be_switched_off_entirely(conn, config):
    """The master switch has to be a real off: no scope resolution, no creation,
    and a recall that never consults the tree."""

    async def scenario():
        off = MemConfig(scope_aware=False)
        _, resolution = resolution_for(conn, off, PROJECT_A, user_id="u")
        assert resolution.status == STATUS_GLOBAL
        assert conn.execute(
            "SELECT COUNT(*) AS n FROM scope"
        ).fetchone()["n"] == 1, "only the root"
        retriever = Retriever(conn, embed, config=off)
        assert await retriever.search("u", "x", scope_context=PROJECT_A) == []

    asyncio.run(scenario())


def test_recall_reports_where_it_resolved_to(conn, config):
    """A narrow result set has to be explainable, or an empty project scope is
    indistinguishable from an empty store."""

    async def scenario():
        _, resolution = resolution_for(conn, config, PROJECT_A, user_id="u")
        add_fact(conn, [resolution.scope_id], obj="某事")
        retriever = Retriever(conn, embed, config=config)
        await retriever.search("u", "某事", scope_context=PROJECT_A)
        assert retriever.last_scope["status"] == STATUS_BOUND
        assert retriever.last_scope["path"].endswith("project:github.com/acme/api")

    asyncio.run(scenario())


# -- validation and conflict layering ---------------------------------------


def _candidate(obj, subject="项目", predicate="规范"):
    return FactCandidate(
        candidate_id=str(uuid.uuid4()), user_id="u", session_id="s",
        subject=subject, predicate=predicate, object=obj, confidence=0.9,
        importance=0.9,
    )


def test_a_cross_scope_neighbour_is_not_a_conflict(conn, config):
    """Same key, different object, different scope: the validator must pass it
    (so both survive) and report the neighbour so the write path can link them."""
    _, resolution_a = resolution_for(conn, config, PROJECT_A, user_id="u")
    _, resolution_b = resolution_for(conn, config, PROJECT_B, user_id="u")
    add_fact(conn, [resolution_b.scope_id], obj="宽松模式")

    candidate = _candidate("严格模式")
    assert validate(candidate, conn).ok is False, "scope-blind: still one pool"
    window_b = [resolution_b.scope_id]
    assert validate(candidate, conn, scope_ids=window_b).ok is False
    window_a = [resolution_a.scope_id]
    assert validate(candidate, conn, scope_ids=window_a).ok is True
    neighbours = cross_scope_neighbours(candidate, conn, window_a)
    assert len(neighbours) == 1 and neighbours[0]["object"] == "宽松模式"


def test_an_unbound_fact_counts_as_global_for_both_directions(conn, config):
    """Pre-scope rows have no binding; treating them as *anything but* global
    would either hide them or make them invisible to cross-scope linking."""
    _, resolution_a = resolution_for(conn, config, PROJECT_A, user_id="u")
    add_fact(conn, [], obj="宽松模式")  # unbound == global
    window_a = [resolution_a.scope_id]
    assert validate(_candidate("严格模式"), conn, scope_ids=window_a).ok is True
    assert cross_scope_neighbours(_candidate("严格模式"), conn, window_a)


# -- evolution and abstraction ----------------------------------------------


def test_relate_cross_scope_names_the_relation(conn, config):
    store = ScopeStore(conn, config)
    client = store.create("client", "acme")
    project = store.create("project", "api", parent_id=client)
    general = add_fact(conn, [GLOBAL_SCOPE_ID], obj="中文")
    specific = add_fact(conn, [project], obj="英文")
    similar = add_fact(conn, [project], obj="中文")

    assert store.relate_cross_scope(specific, project, general, GLOBAL_SCOPE_ID, False) \
        == "exception"
    assert store.relate_cross_scope(similar, project, general, GLOBAL_SCOPE_ID, True) \
        == "cross_scope_similar"
    assert len(store.origins_of(general)) == 1
    assert len(store.evolution_of(general)) == 1


def test_abstraction_requires_independent_scopes(conn, config):
    """The same claim in two scopes is promotable; the same claim twice in one
    scope is not (it would have been deduplicated anyway)."""
    store = ScopeStore(conn, config)
    _, resolution_a = resolution_for(conn, config, PROJECT_A, user_id="u")
    _, resolution_b = resolution_for(conn, config, PROJECT_B, user_id="u")
    claim = "提交信息用中文"
    add_fact(conn, [resolution_a.scope_id], subject="团队", predicate="约定", obj=claim)
    assert store.abstraction_candidates("u", min_scopes=2) == []
    add_fact(conn, [resolution_b.scope_id], subject="团队", predicate="约定", obj=claim)
    candidates = store.abstraction_candidates("u", min_scopes=2)
    assert len(candidates) == 1
    assert sorted(candidates[0]["scope_ids"]) == sorted(
        [resolution_a.scope_id, resolution_b.scope_id]
    )


def test_merge_moves_facts_and_keeps_history(conn, config):
    store = ScopeStore(conn, config)
    client = store.create("client", "acme")
    first = store.create("project", "github.com/acme/api", parent_id=client)
    second = store.create("project", "github.com/acme/api-v2", parent_id=client)
    phase = store.create("phase", "draft", parent_id=second)
    fact_id = add_fact(conn, [second])

    result = store.merge(second, first)
    assert result["facts_moved"] == 1
    assert store.fact_scopes([fact_id])[fact_id] == (first,)
    assert store.get(second).status == "merged"
    assert store.get(second).merged_into == first
    assert store.resolve_id(second) == first
    assert store.path_of(phase).startswith(store.path_of(first))


def test_reparent_corrects_a_tree_discovered_out_of_order(conn, config):
    """The gap merge/split cannot express: a project found before its client was
    ever named sits under the root, and only an explicit move can fix it."""
    store = ScopeStore(conn, config)
    project = store.create("project", "github.com/acme/api")
    client = store.create("client", "acme")
    moved = store.reparent(project, client)
    assert moved.parent_id == client
    assert store.path_of(project) == f"{store.path_of(client)}/project:github.com/acme/api"
    with pytest.raises(ValueError, match="not more general"):
        store.reparent(client, project)
    with pytest.raises(ValueError, match="under itself"):
        store.reparent(client, client)
    with pytest.raises(ValueError, match="global scope"):
        store.reparent(GLOBAL_SCOPE_ID, client)


def test_split_moves_facts_into_a_child_scope(conn, config):
    store = ScopeStore(conn, config)
    project = store.create("project", "github.com/acme/api")
    keep = add_fact(conn, [project], obj="留下")
    move = add_fact(conn, [project], obj="搬走")
    phase = store.split(project, "draft", "phase", [move])
    assert store.fact_scopes([move])[move] == (phase,)
    assert store.fact_scopes([keep])[keep] == (project,)


# -- injection ---------------------------------------------------------------


def test_the_digest_names_the_blocks_it_renders(conn, config):
    store = ScopeStore(conn, config)
    client = store.create("client", "acme")
    project = store.create(
        "project", "github.com/acme/api", parent_id=client,
        signals=[__import__("atom_memory.context", fromlist=["make_signal"]).make_signal(
            "git_remote", "git@github.com:acme/api.git")],
    )
    phase = store.create("phase", "draft", parent_id=project)
    add_fact(conn, [project], subject="项目", predicate="规范", obj="项目规则")
    add_fact(conn, [client], subject="客户", predicate="偏好", obj="正式语气")
    add_fact(conn, [phase], subject="阶段", predicate="要求", obj="先写大纲")
    add_fact(conn, [GLOBAL_SCOPE_ID], subject="全局", predicate="规则", obj="不要提交密钥")

    text = generate_summary(
        conn, "u", 400, detail=False, scope_context=PROJECT_A, config=config
    )
    assert "[当前项目: api · 1 条 · 属性 1]" in text
    assert "[客户: acme · 1 条 · 偏好 1]" in text
    assert "[阶段: draft · 1 条 · 属性 1]" in text
    assert "[全局规则 · 1 条 · 属性 1]" in text
    assert "不要提交密钥" in text
    # The block headings are the point: without them a project rule and a company
    # rule read as two contradicting statements.
    assert text.index("[当前项目: api") < text.index("[全局规则")


def test_the_scoped_digest_never_exceeds_its_budget(conn, config):
    store = ScopeStore(conn, config)
    project = store.create(
        "project", "github.com/acme/api",
        signals=[__import__("atom_memory.context", fromlist=["make_signal"]).make_signal(
            "git_remote", "git@github.com:acme/api.git")],
    )
    for index in range(20):
        add_fact(conn, [project], subject="项目", predicate=f"规范{index}",
                 obj=f"规则{index}")
    for index in range(20):
        add_fact(conn, [GLOBAL_SCOPE_ID], subject="全局", predicate=f"约定{index}",
                 obj=f"约定{index}")
    from atom_memory.retriever import estimate_tokens

    for budget in (60, 90, 120, 300, 800):
        text = generate_summary(
            conn, "u", budget, detail=False, scope_context=PROJECT_A, config=config
        )
        assert estimate_tokens(text) <= budget, f"budget {budget} overshot"


def test_a_block_heading_counts_what_the_block_renders(conn, config):
    """块头里的条数必须等于它下面真正渲染出的条目数。

    The heading used to quote ``len(block.facts)`` — the count the block *started*
    with — while the lines under it were whatever survived the budget. At a tight
    budget the two disagree, and they disagree by more the tighter the squeeze
    gets, which is exactly when a reader trusts the number to see what was lost.
    The heading is now derived from the selection, so the count is checkable
    against the body it sits above.
    """
    from atom_memory.retriever import estimate_tokens

    store = ScopeStore(conn, config)
    project = store.create(
        "project", "github.com/acme/api",
        signals=[__import__("atom_memory.context", fromlist=["make_signal"]).make_signal(
            "git_remote", "git@github.com:acme/api.git")],
    )
    for index in range(12):
        add_fact(conn, [project], subject="项目", predicate=f"规范{index}",
                 obj=f"项目规则{index}")
    for index in range(12):
        add_fact(conn, [GLOBAL_SCOPE_ID], subject="全局", predicate=f"约定{index}",
                 obj=f"全局约定{index}")

    for budget in (150, 250, 400, 800):
        text = generate_summary(
            conn, "u", budget, detail=False, scope_context=PROJECT_A, config=config
        )
        assert estimate_tokens(text) <= budget
        # Split on the block headings; each heading owns the lines up to the next
        # one (or the footer), and its count must match the bullets in that span.
        blocks = re.split(r"^\[(?=[^\]]*\]\s*$)", text, flags=re.M)[1:]
        assert blocks, f"no block headings at budget {budget}: {text!r}"
        for block in blocks:
            heading, _, rest = block.partition("]\n")
            body = rest.split("\n-- ")[0]
            bullets = [line for line in body.splitlines() if line.startswith("- ")]
            claimed = int(re.search(r"· (\d+) 条", heading).group(1))
            assert claimed == len(bullets), (budget, heading, body)


def test_a_block_heading_names_only_the_types_it_renders(conn, config):
    """块头的类型分布只列真正存在于该块的分组，且与正文一致。"""
    store = ScopeStore(conn, config)
    project = store.create(
        "project", "github.com/acme/api",
        signals=[__import__("atom_memory.context", fromlist=["make_signal"]).make_signal(
            "git_remote", "git@github.com:acme/api.git")],
    )
    add_fact(conn, [project], subject="项目", predicate="规范", obj="项目规则")
    add_fact(conn, [GLOBAL_SCOPE_ID], subject="全局", predicate="偏好", obj="正式语气")
    conn.execute(
        "UPDATE facts SET type = 'decision_rule' WHERE predicate = '规范'"
    )
    conn.execute("UPDATE facts SET type = 'lesson' WHERE predicate = '偏好'")
    conn.commit()

    text = generate_summary(
        conn, "u", 400, detail=False, scope_context=PROJECT_A, config=config
    )
    assert "[当前项目: api · 1 条 · 决策规则 1]" in text
    assert "[全局规则 · 1 条 · 教训 1]" in text
    # A type the block does not contain must not be named in its heading.
    assert "偏好" not in text.split("[全局规则")[0]

    # The footer carries the artifact-wide view and no longer says "facts": the
    # numbers are rendered *lines*, and a folded preference list or a to-do list
    # makes "lines" and "facts" differ by construction.
    assert "类型分布" not in text
    assert "条事实" not in text
    assert "-- （按行）" in text


def test_scope_blocks_are_separated_by_a_line_that_survives_injection(conn, config):
    """块之间用一个 ``:`` 行分隔，而不是空行——空行注入后变成 ``| ``。

    The host prefixes every line, blank ones included, so a blank separator
    arrives as a line holding nothing but the prefix. A single colon costs one
    token, reads as structure, and gives the four line kinds four distinct first
    characters under the prefix: ``[`` heading, ``#`` label, ``-`` fact, ``:``
    separator.
    """
    store = ScopeStore(conn, config)
    project = store.create(
        "project", "github.com/acme/api",
        signals=[__import__("atom_memory.context", fromlist=["make_signal"]).make_signal(
            "git_remote", "git@github.com:acme/api.git")],
    )
    add_fact(conn, [project], subject="项目", predicate="规范", obj="项目规则")
    add_fact(conn, [GLOBAL_SCOPE_ID], subject="全局", predicate="规则", obj="不要提交密钥")

    text = generate_summary(
        conn, "u", 400, detail=False, scope_context=PROJECT_A, config=config
    )
    lines = text.splitlines()
    assert ":" in lines
    assert "" not in lines, "a blank line would inject as a bare '| '"
    # The separator sits between the blocks, not inside one.
    assert lines.index(":") > lines.index("## 属性")
    assert lines.index(":") < next(
        index for index, line in enumerate(lines) if line.startswith("[全局规则")
    )


def test_the_current_scope_and_the_global_rules_survive_a_squeeze(conn, config):
    """At a budget too small for everything, the project's own material and the
    global rules are what a reader must still see — the ancestors and phases are
    what a squeeze gives up first."""
    store = ScopeStore(conn, config)
    client = store.create("client", "acme")
    project = store.create(
        "project", "github.com/acme/api", parent_id=client,
        signals=[__import__("atom_memory.context", fromlist=["make_signal"]).make_signal(
            "git_remote", "git@github.com:acme/api.git")],
    )
    phase = store.create("phase", "draft", parent_id=project)
    for index in range(6):
        add_fact(conn, [project], subject="项目", predicate=f"规范{index}",
                 obj=f"项目规则{index}")
        add_fact(conn, [client], subject="客户", predicate=f"偏好{index}",
                 obj=f"客户偏好{index}")
        add_fact(conn, [phase], subject="阶段", predicate=f"要求{index}",
                 obj=f"阶段要求{index}")
        add_fact(conn, [GLOBAL_SCOPE_ID], subject="全局", predicate=f"规则{index}",
                 obj=f"全局规则{index}")
    from atom_memory.retriever import estimate_tokens

    text = generate_summary(
        conn, "u", 90, detail=False, scope_context=PROJECT_A, config=config
    )
    assert estimate_tokens(text) <= 90
    assert "[当前项目: api" in text
    assert "[全局规则" in text


def test_another_projects_phase_is_not_injected_as_this_projects_phase(conn, config):
    """A phase is a phase *of a project*. Rendering a foreign one under a "阶段"
    heading would be cross-project pollution wearing a helpful-looking label."""
    store = ScopeStore(conn, config)
    from atom_memory.context import make_signal

    project_a = store.create(
        "project", "github.com/acme/api",
        signals=[make_signal("git_remote", "git@github.com:acme/api.git")],
    )
    project_b = store.create(
        "project", "github.com/acme/web",
        signals=[make_signal("git_remote", "git@github.com:acme/web.git")],
    )
    own_phase = store.create("phase", "draft", parent_id=project_a)
    foreign_phase = store.create("phase", "launch", parent_id=project_b)
    add_fact(conn, [own_phase], subject="阶段", predicate="要求", obj="自己的阶段")
    add_fact(conn, [foreign_phase], subject="阶段", predicate="要求", obj="别人的阶段")

    text = generate_summary(
        conn, "u", 400, detail=False, scope_context=PROJECT_A, config=config
    )
    assert "自己的阶段" in text
    assert "别人的阶段" not in text


def test_a_condition_matching_fact_from_another_scope_gets_its_own_block(conn, config):
    """The fourth block kind: a rule from somewhere else that applies *here*,
    labelled with the conditions that made it apply."""
    store = ScopeStore(conn, config)
    _, resolution_b = resolution_for(conn, config, PROJECT_B, user_id="u")
    add_fact(conn, [resolution_b.scope_id], subject="报告", predicate="要求",
             obj="先写执行摘要", conditions=[("doc_type", "proposal")])
    add_fact(conn, [resolution_b.scope_id], subject="其他", predicate="规范",
             obj="与提案无关")

    payload = dict(PROJECT_A, conditions={"doc_type": "proposal"})
    text = generate_summary(conn, "u", 400, detail=False, scope_context=payload,
                            config=config)
    assert "[条件规则: doc_type=proposal" in text
    assert "先写执行摘要" in text
    assert "与提案无关" not in text, "no condition match, so not injected"


def test_the_detail_depth_is_not_scope_blocked(conn, config):
    """The detail view exists to locate and edit facts, so it lists everything
    and carries no block headings."""
    _, resolution = resolution_for(conn, config, PROJECT_A, user_id="u")
    add_fact(conn, [resolution.scope_id], obj="某事")
    text = generate_summary(
        conn, "u", 4000, detail=True, scope_context=PROJECT_A, config=config
    )
    assert "[当前项目" not in text
    assert "某事" in text
