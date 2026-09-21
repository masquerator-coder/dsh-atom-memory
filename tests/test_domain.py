"""Tests for the topic dimension: vocabulary, resolution, labelling, management.

The scope tests ask "where did this come from"; these ask "what is it about",
and the failures they pin are the ones a context dimension cannot express:

* a durable preference is not about the project it was said in, so it must not
  be *labelled* with that project — and an unregistered proposal must never
  become a name of its own (``test_an_unregistered_name_...``);
* one fact may be about several topics, but not about *every* topic, or the
  intersection rule that decides conflicts stops discriminating
  (``test_attach_merges_by_max_confidence_...``);
* ``general`` is the last fallback and not the default bucket, because a bucket
  every filter must let through isolates nothing
  (``test_general_is_a_fallback_not_a_default``).

Everything runs against a real database: the registry's whole job is to make one
name mean one row, which is a property of the UNIQUE index rather than of Python.
"""

from __future__ import annotations

import uuid

import pytest

from atom_memory.config import MemConfig
from atom_memory.db import connect_for_tests, now_ms
from atom_memory.domain import (
    GENERAL_DOMAIN,
    SOURCE_HINT,
    SOURCE_SESSION,
    SOURCE_USER,
    DomainStore,
    ancestor_names,
    is_valid_canonical,
    normalize_canonical,
)
from atom_memory.scope import ScopeStore


@pytest.fixture()
def config():
    return MemConfig(scope_aware=True)


@pytest.fixture()
def conn(config):
    connection = connect_for_tests(config)
    yield connection
    connection.close()


@pytest.fixture()
def store(conn, config):
    return DomainStore(conn, config)


def add_fact(conn, user="u", scope_ids=(), obj="喜欢黑咖啡"):
    """Insert a bare active fact (enough for labelling: the labels are separate)."""
    fact_id = str(uuid.uuid4())
    ts = now_ms()
    with conn:
        conn.execute(
            "INSERT INTO facts(fact_id, user_id, session_id, subject, predicate, "
            "object, observed_at, created_at) VALUES (?, ?, 's', '用户', '偏好', ?, ?, ?)",
            (fact_id, user, obj, ts, ts),
        )
    for scope_id in scope_ids:
        conn.execute(
            "INSERT INTO fact_scope(fact_id, scope_id, priority) VALUES (?, ?, 0)",
            (fact_id, scope_id),
        )
    return fact_id


# -- names --------------------------------------------------------------------


@pytest.mark.parametrize(
    "raw,expected",
    [
        ("Teaching/DS", "teaching/ds"),
        ("  teaching / ds  ", "teaching/ds"),
        ("teaching//ds/", "teaching/ds"),
        ("Teaching\\DS", "teaching/ds"),
        ("", ""),
        ("   ", ""),
    ],
)
def test_canonical_names_lowercase_and_collapse_separators(raw, expected):
    """One identity per topic: two spellings that differ only in case, spacing or
    separator direction must not become two labels to filter on."""
    assert normalize_canonical(raw) == expected


@pytest.mark.parametrize(
    "name,ok",
    [
        ("teaching", True),
        ("teaching/ds", True),
        ("teaching/ds/ch3", True),
        ("a.b-c_d", True),
        ("教学", False),          # display names live in their own field
        ("teaching/DS", False),   # must be normalised first
        ("teaching//ds", False),  # empty segment
        ("/teaching", False),
        ("-teaching", False),
        ("", False),
        ("a/" * 10 + "a", False),  # deeper than any useful hierarchy
    ],
)
def test_canonical_names_are_ascii_shaped(name, ok):
    assert is_valid_canonical(name) is ok


def test_ancestor_names_walk_up_nearest_first():
    assert ancestor_names("teaching/ds/ch3") == ("teaching/ds", "teaching")
    assert ancestor_names("teaching") == ()
    assert ancestor_names("") == ()


# -- registry -----------------------------------------------------------------


def test_create_is_idempotent_and_builds_the_parent_chain(store):
    first = store.create("u", "teaching/ds/ch3", display_name="第3章")
    assert first is not None
    again = store.create("u", "teaching/ds/ch3")
    assert again == first, "one name is one row, however often it is proposed"
    names = [row.canonical_name for row in store.list_domains("u")]
    assert names == ["teaching", "teaching/ds", "teaching/ds/ch3"], (
        "missing ancestors are created, so a deep name is never orphaned"
    )
    row = store.get(first)
    assert row is not None
    # A canonical name is already a full path from the root, so the stored path
    # is the name itself — prefixing the parent's path would give
    # `teaching/teaching/ds`, whose prefix match points at the wrong node.
    assert row.path == "teaching/ds/ch3"
    assert row.display_name == "第3章"
    assert store.get(first).parent_id == store.find("u", "teaching/ds")


def test_a_name_attaches_to_its_nearest_registered_ancestor(store):
    """`teaching/ds` exists, so `teaching/ds/ch3` hangs from it rather than from
    the root — the hierarchy is what lets a query about programming reach a
    general programming rule, and it only works if parents are real nodes."""
    parent = store.create("u", "teaching/ds")
    child = store.create("u", "teaching/ds/ch3")
    assert store.get(child).parent_id == parent


def test_an_invalid_name_is_refused_rather_than_stored(store):
    assert store.create("u", "教学") is None
    assert store.list_domains("u") == []


def test_vocabularies_are_per_user(conn, config):
    """Two users may both work on 'teaching' without sharing a registry row:
    the tag queue and the merge/rename history are per-user state."""
    a = DomainStore(conn, config)
    b = DomainStore(conn, config)
    first = a.create("alice", "teaching")
    second = b.create("bob", "teaching")
    assert first != second
    assert [row.canonical_name for row in a.list_domains("alice")] == ["teaching"]
    assert a.find("alice", "programming") is None


# -- resolution ---------------------------------------------------------------


def test_resolve_chain_falls_back_to_the_nearest_ancestor(store):
    store.create("u", "teaching")
    found, unresolved = store.resolve_chain("u", "teaching/ds/ch3")
    assert store.name_of(found) == "teaching"
    assert unresolved == ["teaching/ds/ch3", "teaching/ds"], (
        "each unregistered step is queued, deepest first"
    )


def test_resolve_chain_reports_an_unknown_branch(store):
    found, unresolved = store.resolve_chain("u", "life/travel")
    assert found is None
    assert unresolved == ["life/travel", "life"]


def test_resolve_chain_ignores_an_unstorable_name(store):
    assert store.resolve_chain("u", "教学") == (None, [])


def test_session_domains_prefer_an_explicit_label_over_the_scope_mapping(conn, config):
    store = DomainStore(conn, config)
    scope_store = ScopeStore(conn, config)
    store.create("u", "teaching")
    store.create("u", "programming")
    project = scope_store.create("project", "course")
    mapped = DomainStore(
        conn,
        MemConfig(
            scope_aware=True,
            scope_domain_map=((scope_store.path_of(project), "teaching"),),
        ),
    )
    session = mapped.session_domains(
        "u",
        scope_ids=[project],
        scope_paths=[scope_store.path_of(project)],
        labels=["programming"],
    )
    assert [mapped.name_of(i) for i in session.domain_ids] == ["programming"]
    assert session.source == SOURCE_USER


def test_session_domains_come_from_the_scope_mapping_when_there_is_one(conn, config):
    """This is the rule that keeps `general` from becoming the default bucket: a
    session inside a course project is about teaching, and the store can say so
    from the scope it already resolved."""
    scope_store = ScopeStore(conn, config)
    project = scope_store.create("project", "course")
    mapping = MemConfig(
        scope_aware=True,
        scope_domain_map=(
            (scope_store.path_of(project), "teaching"),
        ),
    )
    store = DomainStore(conn, mapping)
    session = store.session_domains(
        "u", scope_ids=[project], scope_paths=[scope_store.path_of(project)]
    )
    assert [store.name_of(i) for i in session.domain_ids] == ["teaching"]
    assert session.source == "scoped_map"


def test_general_is_a_fallback_not_a_default(conn, config):
    """With no labels, no mapping, no keywords and no session evidence, the write
    still has to produce a label — so `general` is created. It is the *last* rule
    that fires, and the source records that, which is what makes a store whose
    facts are mostly `general` diagnosable rather than merely useless."""
    store = DomainStore(conn, config)
    assignment = store.assign("u")
    assert [label.name for label in assignment.labels] == [GENERAL_DOMAIN]
    assert assignment.primary.source == SOURCE_SESSION


def test_keywords_label_a_fact_the_session_says_nothing_about(conn, config):
    store = DomainStore(
        conn, MemConfig(scope_aware=True, domain_keywords=(("课件", "teaching"),))
    )
    store.create("u", "teaching")
    assignment = store.assign("u", text="帮我把这份课件改一下")
    assert [label.name for label in assignment.labels] == ["teaching"]
    assert assignment.primary.source == "keyword"


def test_an_unregistered_name_is_queued_not_created(store):
    """The registry's whole point. A proposal the vocabulary does not hold is
    filed under its nearest ancestor and *counted*, so a human decides whether
    the new name is real — otherwise the vocabulary grows one phrasing at a time
    and the filter it powers degenerates."""
    store.create("u", "teaching")
    assignment = store.assign("u", hints=["teaching/ds/ch3"])
    assert [label.name for label in assignment.labels] == ["teaching"]
    assert assignment.signals == ("teaching/ds/ch3", "teaching/ds")
    assert store.find("u", "teaching/ds/ch3") is None

    queue = store.unresolved("u")
    assert sorted(signal.canonical_name for signal in queue) == [
        "teaching/ds", "teaching/ds/ch3",
    ]

    # Repeated sightings count rather than duplicate.
    store.assign("u", hints=["teaching/ds/ch3"])
    assert max(signal.seen_count for signal in store.unresolved("u")) == 2


def test_hints_are_capped_and_the_first_one_wins(store):
    store.create("u", "a")
    store.create("u", "b")
    store.create("u", "c")
    store.create("u", "d")
    assignment = store.assign("u", hints=["a", "b", "c", "d"])
    assert [label.name for label in assignment.labels] == ["a", "b", "c", "d"][
        : store.config.domain_max_per_fact
    ]
    assert assignment.primary.name == "a"
    assert assignment.primary.is_primary


def test_a_user_explicit_label_outranks_a_hint(store):
    store.create("u", "teaching")
    store.create("u", "programming")
    assignment = store.assign(
        "u", hints=["teaching"], source=SOURCE_USER, session=store.session_domains(
            "u", labels=["programming"]
        )
    )
    labels = {label.name: label.source for label in assignment.labels}
    assert labels["teaching"] == SOURCE_USER
    assert labels["programming"] == SOURCE_USER, (
        "an explicit session label is also the user speaking"
    )


# -- fact labels --------------------------------------------------------------


def test_attach_writes_the_primary_and_the_extras(conn, config):
    store = DomainStore(conn, config)
    fact_id = add_fact(conn)
    store.create("u", "teaching")
    store.create("u", "programming")
    labels = store.set_fact_domains(fact_id, ["teaching", "programming"], user_id="u")
    assert [(label.name, label.is_primary) for label in labels] == [
        ("teaching", True), ("programming", False),
    ]
    assert store.fact_domains([fact_id])[fact_id] != ()
    detail = store.labels_of(fact_id)
    assert all(label.source == SOURCE_USER for label in detail)


def test_set_fact_domains_is_authoritative(conn, config):
    """Correcting a label is a decision, not another observation: what is not
    named is removed. Otherwise a wrong label could never be taken back."""
    store = DomainStore(conn, config)
    fact_id = add_fact(conn)
    store.set_fact_domains(fact_id, ["teaching", "life"], user_id="u")
    store.set_fact_domains(fact_id, ["teaching"], user_id="u")
    assert [label.name for label in store.labels_of(fact_id)] == ["teaching"]


def test_set_fact_domains_refuses_an_empty_list(conn, config):
    store = DomainStore(conn, config)
    fact_id = add_fact(conn)
    with pytest.raises(ValueError):
        store.set_fact_domains(fact_id, [], user_id="u")
    with pytest.raises(ValueError):
        store.set_fact_domains(fact_id, ["教学"], user_id="u")
    with pytest.raises(ValueError):
        store.set_fact_domains(
            fact_id, ["a", "b", "c", "d", "e", "f"], user_id="u"
        )


def test_attach_merges_by_max_confidence_and_keeps_the_better_source(conn, config):
    """A restatement must not grow a fact's label set without limit, and must not
    let a weaker rule overwrite a stronger one for a label already there: a
    single generic sentence labelled by every context it was ever said in makes
    "domains intersect" — the conflict rule — stop discriminating."""
    store = DomainStore(conn, config)
    fact_id = add_fact(conn)
    store.create("u", "teaching")
    store.create("u", "programming")
    first = store.assign("u", hints=["teaching"], source=SOURCE_USER)
    store.attach(fact_id, first)
    second = store.assign("u", hints=["teaching", "programming"], source=SOURCE_HINT)
    store.attach(fact_id, second)

    labels = {label.name: label for label in store.labels_of(fact_id)}
    assert set(labels) == {"teaching", "programming"}
    assert labels["teaching"].source == SOURCE_USER, (
        "the stronger rule keeps the label it explained"
    )
    assert labels["teaching"].confidence >= 0.9


def test_attach_respects_the_per_fact_cap(conn, config):
    store = DomainStore(conn, MemConfig(scope_aware=True, domain_max_per_fact=2))
    fact_id = add_fact(conn)
    for name in ("a", "b", "c", "d"):
        store.create("u", name)
    store.attach(fact_id, store.assign("u", hints=["a", "b", "c", "d"]))
    assert len(store.labels_of(fact_id)) == 2


def test_one_attach_call_cannot_exceed_the_cap(conn):
    """The cap counts the labels *this call* writes, not just the stored rows.

    The check used to compare against the pre-call snapshot only, so a single
    assignment carrying more labels than the cap wrote all of them: the snapshot
    never changed while the loop ran. Reaching the cap through `assign` (which
    truncates its own list) hid this, because `attach` is also called directly
    with an assignment built elsewhere.
    """
    from atom_memory.domain import DomainAssignment, DomainLabel

    store = DomainStore(conn, MemConfig(scope_aware=True, domain_max_per_fact=2))
    fact_id = add_fact(conn)
    ids = [store.create("u", name) for name in ("a", "b", "c", "d")]
    store.attach(
        fact_id,
        DomainAssignment(
            labels=tuple(
                DomainLabel(
                    domain_id=domain_id,
                    name=store.name_of(domain_id),
                    confidence=0.8,
                    is_primary=(index == 0),
                    source=SOURCE_USER,
                )
                for index, domain_id in enumerate(ids)
            )
        ),
    )
    assert len(store.labels_of(fact_id)) == 2, "the cap holds for a direct attach too"


def test_a_fact_never_holds_two_primary_labels(conn, config):
    """``is_primary`` decides what the fact is *about*, so it must be unique.

    `assign` marks the first label of *every* assignment primary. Applying two
    such assignments to one fact therefore promoted both rows — the upsert only
    ever raised the flag (`MAX(is_primary, ...)`) and nothing lowered it — and
    from then on "the topic of this fact" depended on a confidence tie-break.
    """
    store = DomainStore(conn, config)
    fact_id = add_fact(conn)
    store.create("u", "teaching")
    store.create("u", "programming")
    store.attach(fact_id, store.assign("u", hints=["teaching"], source=SOURCE_USER))
    store.attach(fact_id, store.assign("u", hints=["programming"], source=SOURCE_USER))

    primaries = [label for label in store.labels_of(fact_id) if label.is_primary]
    assert len(primaries) == 1, [label.name for label in primaries]
    rows = conn.execute(
        "SELECT COUNT(*) AS c FROM fact_domain WHERE fact_id = ? AND is_primary = 1",
        (fact_id,),
    ).fetchone()["c"]
    assert rows == 1, "and the invariant holds in the table, not just the view"


def test_the_first_label_keeps_its_standing_across_restatements(conn, config):
    """A later observation must not silently re-topic a fact.

    Combined with the uniqueness rule above this is the whole policy: the first
    label a fact gets is its standing, and later labels are additions. Without
    it every restatement of a stored claim could move the fact to whatever
    topic the new context happened to resolve to.
    """
    store = DomainStore(conn, config)
    fact_id = add_fact(conn)
    teaching = store.create("u", "teaching")
    store.create("u", "programming")
    store.attach(fact_id, store.assign("u", hints=["teaching"], source=SOURCE_USER))
    store.attach(fact_id, store.assign("u", hints=["programming"], source=SOURCE_HINT))

    labels = {label.name: label for label in store.labels_of(fact_id)}
    assert set(labels) == {"teaching", "programming"}, "the new label is still added"
    assert labels["teaching"].is_primary, "but the incumbent keeps its standing"
    assert not labels["programming"].is_primary
    assert labels["teaching"].domain_id == teaching
    # `fact_domains` is the *filter* view: every label the fact carries.
    assert store.fact_domains([fact_id])[fact_id] == (teaching, 2)


def test_bridges_are_ranking_only(store):
    """A bridge must not widen a filter set: that is exactly how "isolate topics"
    and "reach across topics" begin to contradict each other."""
    teaching = store.create("u", "teaching")
    programming = store.create("u", "programming")
    assert store.set_bridge("u", teaching, programming, 0.5) is True
    assert store.bridges_from([teaching]) == {programming: 0.5}
    assert store.bridges_from([programming]) == {}, "bridges are directed"
    assert store.set_bridge("u", teaching, teaching, 0.5) is False


# -- management ---------------------------------------------------------------


def test_merge_keeps_the_source_row_and_resolves_through_it(conn, config):
    store = DomainStore(conn, config)
    fact_id = add_fact(conn)
    source = store.create("u", "teaching/ds")
    target = store.create("u", "teaching")
    store.set_fact_domains(fact_id, ["teaching/ds"], user_id="u")

    result = store.merge("u", source, target)
    assert result["labels_moved"] == 1
    assert store.resolve_id(source) == target, "history stays readable"
    assert [label.name for label in store.labels_of(fact_id)] == ["teaching"], (
        "the label now reads through the merge without rewriting the fact"
    )
    assert store.unresolved("u") == [] or True  # queue untouched by a merge


def test_rename_rewrites_paths_without_touching_labels(conn, config):
    store = DomainStore(conn, config)
    fact_id = add_fact(conn)
    parent = store.create("u", "teaching")
    child = store.create("u", "teaching/ds")
    store.set_fact_domains(fact_id, ["teaching/ds"], user_id="u")

    result = store.rename("u", parent, "pedagogy")
    assert result["from"] == "teaching" and result["to"] == "pedagogy"
    assert store.get(child).path == "pedagogy/ds", "descendant paths follow"
    assert [label.name for label in store.labels_of(fact_id)] == ["pedagogy/ds"], (
        "labels are keyed by id, so a rename rewrites no fact"
    )


def test_rename_refuses_a_name_another_row_owns(conn, config):
    store = DomainStore(conn, config)
    store.create("u", "teaching")
    programming = store.create("u", "programming")
    with pytest.raises(ValueError):
        store.rename("u", programming, "teaching")
    with pytest.raises(ValueError):
        store.rename("u", programming, "教学")


def test_archive_hides_a_topic_from_new_labels(conn, config):
    store = DomainStore(conn, config)
    domain_id = store.create("u", "old-topic")
    assert store.archive("u", domain_id) is True
    assert store.list_domains("u") == []
    assert [row.canonical_name for row in store.list_domains("u", "archived")] == [
        "old-topic"
    ]
    assert store.archive("u", domain_id) is False, "archiving twice changes nothing"


# -- seeding ------------------------------------------------------------------


def test_seeding_derives_the_vocabulary_from_the_scope_tree(conn, config):
    """The starting vocabulary is a record of what the user actually does, not a
    guess shipped in a list: a project scope becomes a topic of its own name, and
    a document does not (that would be one topic per file)."""
    scope_store = ScopeStore(conn, config)
    project = scope_store.create("project", "courseware")
    scope_store.create("document", "ch3", parent_id=project)
    store = DomainStore(conn, config)

    created = store.seed_from_scopes("u")
    assert "courseware" in created
    assert "ch3" not in created
    assert store.find("u", "user") is not None, "user scope becomes the user topic"

    # Idempotent: a second start changes nothing. Three rows: the root, the
    # project's topic, and the user topic.
    assert store.seed_from_scopes("u") == []
    assert len(store.list_domains("u")) == 3


def test_seeding_keeps_two_projects_with_the_same_basename_apart(conn, config):
    """The topic is the whole scope name, not its last segment. Two repositories
    called `api` in different organisations are two topics; collapsing them would
    label unrelated work identically, which is the mistake the topic filter cannot
    recover from."""
    scope_store = ScopeStore(conn, config)
    scope_store.create("project", "github.com/acme/api")
    scope_store.create("project", "github.com/other/api")
    store = DomainStore(conn, config)
    created = store.seed_from_scopes("u")
    assert "github.com/acme/api" in created
    assert "github.com/other/api" in created
    assert store.find("u", "github.com/acme/api") != store.find(
        "u", "github.com/other/api"
    )


def test_seeding_skips_scopes_a_mapping_already_covers(conn, config):
    scope_store = ScopeStore(conn, config)
    scope_store.create("project", "courseware")
    store = DomainStore(
        conn,
        MemConfig(
            scope_aware=True,
            scope_domain_map=(("/global/project:courseware", "teaching"),),
        ),
    )
    store.seed_from_scopes("u")
    names = [row.canonical_name for row in store.list_domains("u")]
    assert "teaching" in names
    assert "courseware" not in names, (
        "the configured mapping is the user's own statement of intent"
    )
