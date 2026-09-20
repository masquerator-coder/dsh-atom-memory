"""Tests for the work overview (``overview.py``).

Three things are under test, and each exists to stop a specific failure the
design would otherwise have:

* **the changelog classifier** — ``change_level`` must distinguish "a detail
  moved" from "the shape of the work changed", because that distinction is the
  only thing standing between a model call per message and a model call per real
  change;
* **the fingerprint** — it must be sensitive to every input the overview's text
  depends on (including ``last_used_at``, which changes which highlights rank
  first) and *insensitive* to everything else, or the cache either serves stale
  text or is regenerated for nothing;
* **the cache and the refresh gate** — ``read_overview`` refuses a mismatched
  fingerprint, ``write_overview`` is idempotent and refuses to store nothing, and
  ``should_refresh`` combines the two signals the way the out-of-band job relies
  on.

The aggregation itself is tested for grouping (by work unit, not by type), the
global bucket, rank-ordered highlights and the character bounds.
"""

from __future__ import annotations

import json

import pytest

from atom_memory.config import MemConfig
from atom_memory.db import connect_for_tests, record_event
from atom_memory.models import (
    TYPE_DECISION_RULE,
    TYPE_EPISODIC,
    TYPE_LESSON,
    TYPE_SEMANTIC,
)
from atom_memory.overview import (
    LEVEL_DETAIL,
    LEVEL_NONE,
    LEVEL_RESET,
    LEVEL_STRUCTURAL,
    build_overview_skeleton,
    change_level,
    level_at_least,
    overview_fingerprint,
    overview_status,
    read_overview,
    recent_changes,
    render_skeleton_overview,
    should_refresh,
    write_overview,
)
from atom_memory.scope import ScopeStore
from atom_memory.retriever import estimate_tokens


def _add_fact(
    conn,
    fact_id: str,
    memory_type: str = TYPE_SEMANTIC,
    subject: str = "s",
    predicate: str = "p",
    obj: str = "o",
    created_at: int = 1000,
    last_used_at: int | None = None,
    scope_id: int | None = None,
    user_id: str = "u1",
):
    """Insert one active fact directly, bypassing the worker pipeline."""
    conn.execute(
        "INSERT INTO facts(fact_id, user_id, session_id, subject, predicate, "
        "object, confidence, importance, type, status, observed_at, created_at, "
        "last_used_at, version) "
        "VALUES (?, ?, 's1', ?, ?, ?, 0.5, 0.5, ?, 'active', ?, ?, ?, 1)",
        (fact_id, user_id, subject, predicate, obj, memory_type,
         created_at, created_at, last_used_at),
    )
    if scope_id is not None:
        conn.execute(
            "INSERT INTO fact_scope(fact_id, scope_id, priority) VALUES (?, ?, 0)",
            (fact_id, scope_id),
        )
    conn.commit()


@pytest.fixture()
def env():
    """A connection with one project scope available for binding."""
    conn = connect_for_tests()
    store = ScopeStore(conn, MemConfig())
    project = store.create("project", "demo", parent_id=1, confidence=1.0)
    try:
        yield conn, project
    finally:
        conn.close()


# ---- change levels -----------------------------------------------------------


def test_nothing_since_the_bound_is_no_change(env):
    conn, _project = env
    assert change_level(conn, "u1", 0) == LEVEL_NONE


def test_an_attribute_write_is_only_a_detail(env):
    """A tweaked attribute must not justify a model call."""
    conn, project = env
    record_event(
        conn, "fact_written",
        {"type": TYPE_SEMANTIC, "scope_id": project}, user_id="u1",
    )
    assert change_level(conn, "u1", 0) == LEVEL_DETAIL


def test_a_durable_write_is_structural(env):
    """A new decision rule changes what the user has worked on."""
    conn, project = env
    record_event(
        conn, "fact_written",
        {"type": TYPE_DECISION_RULE, "scope_id": project}, user_id="u1",
    )
    assert change_level(conn, "u1", 0) == LEVEL_STRUCTURAL


def test_an_episodic_write_is_not_structural(env):
    """The extractor's own prompt calls session narration something not to save.

    Whatever trickles through anyway must be treated as a detail: making a
    one-off event trigger a rewrite would let conversation noise drive the
    overview.
    """
    conn, _project = env
    record_event(conn, "fact_written", {"type": TYPE_EPISODIC}, user_id="u1")
    assert change_level(conn, "u1", 0) == LEVEL_DETAIL


def test_retirement_and_reorganisation_are_structural(env):
    conn, _project = env
    for event_type in ("fact_superseded", "facts_archived", "scope_promoted"):
        conn.execute("DELETE FROM events WHERE user_id = 'u1'")
        record_event(conn, event_type, {}, user_id="u1")
        assert change_level(conn, "u1", 0) == LEVEL_STRUCTURAL, event_type


def test_a_purge_outranks_everything(env):
    """``reset`` wins even when a structural event is also present."""
    conn, _project = env
    record_event(conn, "fact_written", {"type": TYPE_DECISION_RULE}, user_id="u1")
    record_event(conn, "facts_purged", {}, user_id="u1")
    assert change_level(conn, "u1", 0) == LEVEL_RESET


def test_a_malformed_payload_degrades_to_a_detail(env):
    """The changelog is written by another process; parsing must not raise."""
    conn, _project = env
    conn.execute(
        "INSERT INTO events(event_id, user_id, type, payload, created_at) "
        "VALUES ('e1', 'u1', 'fact_written', 'not json', 1000)"
    )
    conn.commit()
    assert change_level(conn, "u1", 0) == LEVEL_DETAIL


def test_the_boundary_millisecond_counts_as_a_change(env):
    """A change written in the cache's own millisecond is still a change.

    Timestamps are whole milliseconds, so a fast refresh writes the cache and its
    follow-up event in the same tick. Excluding the boundary made that change
    invisible to the classifier — the same decision reached by the wrong route.
    The cache's timestamp is forced to the event's rather than relying on the two
    landing in one millisecond: the property under test is the comparison, not
    the clock's resolution.
    """
    conn, _project = env
    _add_fact(conn, "f1", obj="a")
    write_overview(conn, "u1", "text", "fp", 1)
    record_event(conn, "fact_written", {"type": TYPE_DECISION_RULE}, user_id="u1")
    event_at = conn.execute(
        "SELECT created_at FROM events WHERE user_id = 'u1'"
    ).fetchone()["created_at"]
    conn.execute(
        "UPDATE memory_overview SET created_at = ? WHERE user_id = 'u1'", (event_at,)
    )
    conn.commit()

    assert change_level(conn, "u1", event_at) == LEVEL_STRUCTURAL
    assert should_refresh(conn, "u1")[0] is True


def test_changes_are_isolated_per_user(env):
    conn, _project = env
    record_event(conn, "facts_purged", {}, user_id="other")
    assert change_level(conn, "u1", 0) == LEVEL_NONE


def test_level_at_least_orders_severity():
    assert level_at_least(LEVEL_STRUCTURAL, LEVEL_STRUCTURAL)
    assert level_at_least(LEVEL_RESET, LEVEL_STRUCTURAL)
    assert not level_at_least(LEVEL_DETAIL, LEVEL_STRUCTURAL)
    assert level_at_least(LEVEL_DETAIL, LEVEL_NONE)
    # An unknown level ranks as "nothing", so a new event type can never raise
    # the level past a threshold by accident.
    assert not level_at_least("something_new", LEVEL_DETAIL)
    assert level_at_least(LEVEL_DETAIL, "something_new")


# ---- the aggregation ---------------------------------------------------------


def test_facts_group_by_work_unit_not_by_type(env):
    """The whole point: one project's facts form one unit, not four sections."""
    conn, project = env
    _add_fact(conn, "f1", TYPE_DECISION_RULE, obj="决定A", scope_id=project)
    _add_fact(conn, "f2", TYPE_LESSON, obj="教训B", scope_id=project)
    _add_fact(conn, "f3", TYPE_SEMANTIC, obj="属性C", scope_id=project)

    skeleton = build_overview_skeleton(conn, "u1")
    assert len(skeleton["units"]) == 1
    unit = skeleton["units"][0]
    assert unit["facts"] == 3
    assert unit["type"] == "project"
    assert unit["by_type"] == {
        TYPE_DECISION_RULE: 1, TYPE_LESSON: 1, TYPE_SEMANTIC: 1,
    }


def test_unbound_facts_become_the_global_unit(env):
    """A global rule is still work — it must not vanish from the overview."""
    conn, _project = env
    _add_fact(conn, "f1", TYPE_DECISION_RULE, obj="全局规则")
    skeleton = build_overview_skeleton(conn, "u1")
    assert [unit["type"] for unit in skeleton["units"]] == ["global"]
    assert skeleton["units"][0]["label"] == "全局"


def test_highlights_are_rank_ordered_and_typed(env):
    """A decision rule outranks an attribute, so it leads the highlights."""
    conn, project = env
    _add_fact(conn, "f1", TYPE_SEMANTIC, obj="普通属性", scope_id=project)
    _add_fact(conn, "f2", TYPE_DECISION_RULE, obj="关键决定", scope_id=project)

    skeleton = build_overview_skeleton(conn, "u1")
    assert skeleton["units"][0]["highlights"][0] == "关键决定"


def test_highlights_per_unit_is_respected(env):
    conn, project = env
    for index in range(6):
        _add_fact(conn, f"f{index}", obj=f"条目{index}", scope_id=project)
    skeleton = build_overview_skeleton(conn, "u1", highlights_per_unit=2)
    assert len(skeleton["units"][0]["highlights"]) == 2


def test_material_units_come_before_thin_ones(env):
    conn, project = env
    store = ScopeStore(conn, MemConfig())
    other = store.create("project", "small", parent_id=1, confidence=1.0)
    for index in range(5):
        _add_fact(conn, f"big{index}", obj=f"大{index}", scope_id=project)
    _add_fact(conn, "small1", obj="小", scope_id=other)

    skeleton = build_overview_skeleton(conn, "u1")
    assert [unit["label"] for unit in skeleton["units"]] == ["demo", "small"]


def test_max_units_caps_and_flags_truncation(env):
    conn, _project = env
    store = ScopeStore(conn, MemConfig())
    for index in range(5):
        scope_id = store.create("project", f"proj{index}", parent_id=1, confidence=1.0)
        _add_fact(conn, f"f{index}", obj=f"条目{index}", scope_id=scope_id)

    skeleton = build_overview_skeleton(conn, "u1", max_units=2)
    assert len(skeleton["units"]) == 2
    assert skeleton["truncated"] is True


def test_an_empty_store_yields_no_units_and_a_stable_digest(env):
    conn, _project = env
    skeleton = build_overview_skeleton(conn, "u1")
    assert skeleton["units"] == []
    assert skeleton["totals"]["facts"] == 0
    assert skeleton["fingerprint"] == overview_fingerprint(conn, "u1")
    assert render_skeleton_overview(skeleton) == ""


def test_the_skeleton_is_json_serializable(env):
    """It crosses the RPC boundary as JSON, so nothing private may leak in.

    The aggregation collects per-fact rows while grouping; those must not survive
    into the returned dict, or the skeleton carries every object string in the
    store and the bound on its size is a fiction.
    """
    conn, project = env
    _add_fact(conn, "f1", TYPE_DECISION_RULE, obj="决定A", scope_id=project)
    skeleton = build_overview_skeleton(conn, "u1")
    encoded = json.dumps(skeleton, ensure_ascii=False)
    assert "_candidates" not in encoded
    assert json.loads(encoded)["totals"]["facts"] == 1


def test_facts_are_isolated_per_user(env):
    conn, project = env
    _add_fact(conn, "f1", obj="mine", scope_id=project)
    _add_fact(conn, "f2", obj="theirs", scope_id=project, user_id="other")
    skeleton = build_overview_skeleton(conn, "u1")
    assert skeleton["totals"]["facts"] == 1


def test_retracted_facts_are_excluded(env):
    conn, project = env
    _add_fact(conn, "f1", obj="有效", scope_id=project)
    _add_fact(conn, "f2", obj="已退役", scope_id=project)
    conn.execute("UPDATE facts SET status = 'superseded' WHERE fact_id = 'f2'")
    conn.commit()
    skeleton = build_overview_skeleton(conn, "u1")
    assert skeleton["totals"]["facts"] == 1


# ---- the fallback render -----------------------------------------------------


def test_the_fallback_render_names_units_and_highlights(env):
    conn, project = env
    _add_fact(conn, "f1", TYPE_DECISION_RULE, obj="关键决定", scope_id=project)
    text = render_skeleton_overview(build_overview_skeleton(conn, "u1"))
    assert "demo" in text
    assert "关键决定" in text
    assert text.startswith("- ")


def test_the_fallback_render_respects_its_character_cap(env):
    conn, project = env
    for index in range(20):
        _add_fact(conn, f"f{index}", obj="条目" * 20, scope_id=project)
    text = render_skeleton_overview(build_overview_skeleton(conn, "u1"), max_chars=60)
    assert len(text) <= 60
    # It stops at a whole unit rather than emitting a severed line: the render
    # sheds highlights and then the breakdown before it ever truncates text.
    assert not text.endswith("…")


def test_the_fallback_render_respects_a_token_cap(env):
    """A token cap is honoured by *building* to it, not by clipping after.

    Dense Chinese makes the two caps disagree — the same text can be several
    times its character count in tokens — so a render built to a character cap
    and then shrunk costs its structure and comes out as one severed line.
    """
    conn, project = env
    for index in range(20):
        _add_fact(conn, f"f{index}", obj="条目" * 20, scope_id=project)
    skeleton = build_overview_skeleton(conn, "u1")
    for budget in (5, 12, 30, 80, 200):
        text = render_skeleton_overview(skeleton, max_tokens=budget)
        assert estimate_tokens(text) <= budget, (budget, text)


def test_the_fallback_render_degrades_to_unit_names(env):
    """At a tiny budget the *names* survive, which is the useful part."""
    conn, project = env
    for index in range(20):
        _add_fact(conn, f"f{index}", obj="条目" * 20, scope_id=project)
    text = render_skeleton_overview(build_overview_skeleton(conn, "u1"), max_tokens=8)
    assert "demo" in text
    assert estimate_tokens(text) <= 8


def test_the_breakdown_leads_with_durable_kinds(env):
    """``决策规则 1 · 属性 9`` reads; ``属性 9 · 决策规则 1`` buries the signal."""
    conn, project = env
    _add_fact(conn, "f1", TYPE_DECISION_RULE, obj="决定", scope_id=project)
    for index in range(9):
        _add_fact(conn, f"a{index}", obj=f"属性{index}", scope_id=project)
    text = render_skeleton_overview(build_overview_skeleton(conn, "u1"))
    assert text.index("决策规则") < text.index("属性")


# ---- the fingerprint ---------------------------------------------------------


def test_a_new_fact_moves_the_digest(env):
    conn, project = env
    _add_fact(conn, "f1", obj="a", scope_id=project)
    before = overview_fingerprint(conn, "u1")
    _add_fact(conn, "f2", obj="b", scope_id=project)
    assert overview_fingerprint(conn, "u1") != before


def test_a_new_scope_moves_the_digest(env):
    conn, project = env
    _add_fact(conn, "f1", obj="a", scope_id=project)
    before = overview_fingerprint(conn, "u1")
    store = ScopeStore(conn, MemConfig())
    other = store.create("project", "second", parent_id=1, confidence=1.0)
    _add_fact(conn, "f2", obj="b", scope_id=other)
    assert overview_fingerprint(conn, "u1") != before


def test_reuse_moves_the_digest(env):
    """``last_used_at`` decides which highlights rank first, so it is an input.

    Not because reuse changes what was worked on — it does not — but because it
    changes which entries the overview leads with. A digest blind to it would
    serve highlights chosen under an older ranking.
    """
    conn, project = env
    _add_fact(conn, "f1", obj="a", scope_id=project)
    before = overview_fingerprint(conn, "u1")
    conn.execute("UPDATE facts SET last_used_at = 999999 WHERE fact_id = 'f1'")
    conn.commit()
    assert overview_fingerprint(conn, "u1") != before


def test_retiring_a_fact_moves_the_digest(env):
    conn, project = env
    _add_fact(conn, "f1", obj="a", scope_id=project)
    _add_fact(conn, "f2", obj="b", scope_id=project)
    before = overview_fingerprint(conn, "u1")
    conn.execute("UPDATE facts SET status = 'superseded' WHERE fact_id = 'f2'")
    conn.commit()
    assert overview_fingerprint(conn, "u1") != before


def test_the_digest_is_stable_for_an_unchanged_store(env):
    conn, project = env
    _add_fact(conn, "f1", obj="a", scope_id=project)
    assert overview_fingerprint(conn, "u1") == overview_fingerprint(conn, "u1")


def test_an_empty_store_has_a_stable_digest(env):
    conn, _project = env
    assert overview_fingerprint(conn, "u1") == overview_fingerprint(conn, "u1")


def test_the_digest_is_isolated_per_user(env):
    conn, project = env
    _add_fact(conn, "f1", obj="a", scope_id=project, user_id="u1")
    _add_fact(conn, "f2", obj="b", scope_id=project, user_id="other")
    empty = connect_for_tests()
    try:
        assert overview_fingerprint(empty, "nobody") != overview_fingerprint(conn, "u1")
    finally:
        empty.close()


# ---- the cache ---------------------------------------------------------------


def test_reading_with_a_matching_digest_returns_the_text(env):
    conn, _project = env
    _add_fact(conn, "f1", obj="a")
    fingerprint = overview_fingerprint(conn, "u1")
    write_overview(conn, "u1", "总览正文", fingerprint, 1)
    assert read_overview(conn, "u1", fingerprint) == "总览正文"


def test_reading_with_a_stale_digest_returns_nothing(env):
    """A text that no longer matches the store is treated as absent."""
    conn, _project = env
    write_overview(conn, "u1", "总览正文", "some-other-digest", 0)
    assert read_overview(conn, "u1", overview_fingerprint(conn, "u1")) is None


def test_reading_without_a_digest_returns_whatever_is_cached(env):
    """The panel wants to show the last text even when it is stale."""
    conn, _project = env
    write_overview(conn, "u1", "总览正文", "some-other-digest", 0)
    assert read_overview(conn, "u1") == "总览正文"


def test_reading_an_unwritten_cache_returns_nothing(env):
    conn, _project = env
    assert read_overview(conn, "u1") is None


def test_writing_is_idempotent_and_keeps_one_row(env):
    """One row per user: the table answers "what is current", not "what was"."""
    conn, _project = env
    for index in range(3):
        write_overview(conn, "u1", f"正文{index}", f"fp{index}", index)
    rows = conn.execute(
        "SELECT text, facts_count FROM memory_overview WHERE user_id = 'u1'"
    ).fetchall()
    assert len(rows) == 1
    assert rows[0]["text"] == "正文2"
    assert rows[0]["facts_count"] == 2


def test_writing_nothing_is_refused(env):
    """A generation that produced nothing must not erase a good overview."""
    conn, _project = env
    write_overview(conn, "u1", "好的总览", "fp1", 1)
    for empty in ("", "   ", "\n\t "):
        write_overview(conn, "u1", empty, "fp2", 1)
    assert read_overview(conn, "u1") == "好的总览"


def test_the_cache_is_isolated_per_user(env):
    conn, _project = env
    write_overview(conn, "u1", "我的", "fp", 0)
    write_overview(conn, "other", "别人的", "fp", 0)
    assert read_overview(conn, "u1") == "我的"
    assert read_overview(conn, "other") == "别人的"


# ---- status and the refresh gate --------------------------------------------


def test_status_reports_an_uncached_store(env):
    conn, _project = env
    status = overview_status(conn, "u1")
    assert status["cached"] is False
    assert status["stale"] is True
    assert status["level"] == LEVEL_NONE


def test_status_marks_a_matching_cache_current(env):
    conn, _project = env
    _add_fact(conn, "f1", obj="a")
    fingerprint = overview_fingerprint(conn, "u1")
    write_overview(conn, "u1", "正文", fingerprint, 1)
    status = overview_status(conn, "u1")
    assert status["cached"] is True
    assert status["stale"] is False
    assert status["facts_count"] == 1


def test_status_marks_a_moved_store_stale(env):
    conn, project = env
    _add_fact(conn, "f1", obj="a", scope_id=project)
    fingerprint = overview_fingerprint(conn, "u1")
    write_overview(conn, "u1", "正文", fingerprint, 1)
    _add_fact(conn, "f2", obj="b", scope_id=project)
    status = overview_status(conn, "u1")
    assert status["stale"] is True
    assert status["fingerprint"] != status["current_fingerprint"]


def test_status_reports_the_changelog_level_since_the_cache(env):
    conn, project = env
    _add_fact(conn, "f1", obj="a", scope_id=project)
    write_overview(conn, "u1", "正文", overview_fingerprint(conn, "u1"), 1)
    record_event(conn, "fact_written", {"type": TYPE_DECISION_RULE}, user_id="u1")
    assert overview_status(conn, "u1")["level"] == LEVEL_STRUCTURAL


def test_an_empty_store_is_never_refreshed(env):
    """There is nothing to narrate; caching an overview of nothing would inject
    a block that says nothing."""
    conn, _project = env
    should, reason = should_refresh(conn, "u1")
    assert should is False
    assert reason == "no_facts"


def test_an_uncached_store_with_facts_is_refreshed(env):
    conn, project = env
    _add_fact(conn, "f1", obj="a", scope_id=project)
    should, reason = should_refresh(conn, "u1")
    assert should is True
    assert reason == "not_cached"


def test_a_fingerprint_drift_alone_does_not_refresh(env):
    """The rule that keeps the model call off the path of every message.

    A new attribute moves the fingerprint (the fact count changed) but not the
    shape of the work, so it must not trigger a regeneration. Gating on the
    fingerprint here was a real bug: it made every write a regeneration, which is
    exactly the busywork this gate exists to absorb.
    """
    conn, project = env
    _add_fact(conn, "f1", obj="a", scope_id=project)
    cached_fingerprint = overview_fingerprint(conn, "u1")
    write_overview(conn, "u1", "正文", cached_fingerprint, 1)
    _add_fact(conn, "f2", obj="b", scope_id=project)
    record_event(conn, "fact_written", {"type": TYPE_SEMANTIC}, user_id="u1")

    # The digest did move — the fact count changed — and the gate still says no.
    assert overview_fingerprint(conn, "u1") != cached_fingerprint
    should, reason = should_refresh(conn, "u1")
    assert should is False
    assert reason == "up_to_date"


def test_a_structural_change_refreshes(env):
    conn, project = env
    _add_fact(conn, "f1", obj="a", scope_id=project)
    write_overview(conn, "u1", "正文", overview_fingerprint(conn, "u1"), 1)
    record_event(conn, "fact_written", {"type": TYPE_DECISION_RULE}, user_id="u1")
    should, reason = should_refresh(conn, "u1")
    assert should is True
    assert reason == "level"


def test_the_refresh_threshold_is_configurable(env):
    conn, project = env
    _add_fact(conn, "f1", obj="a", scope_id=project)
    write_overview(conn, "u1", "正文", overview_fingerprint(conn, "u1"), 1)
    record_event(conn, "fact_written", {"type": TYPE_SEMANTIC}, user_id="u1")
    assert should_refresh(conn, "u1", min_level=LEVEL_DETAIL)[0] is True
    assert should_refresh(conn, "u1", min_level=LEVEL_RESET)[0] is False


def test_a_discarded_change_keeps_the_cached_text_readable(env):
    """Detail-only changes neither regenerate nor destroy: the text is still
    there for a human, and reported as stale."""
    conn, project = env
    _add_fact(conn, "f1", obj="a", scope_id=project)
    fingerprint = overview_fingerprint(conn, "u1")
    write_overview(conn, "u1", "总览正文", fingerprint, 1)
    _add_fact(conn, "f2", obj="b", scope_id=project)
    record_event(conn, "fact_written", {"type": TYPE_SEMANTIC}, user_id="u1")

    assert should_refresh(conn, "u1")[0] is False
    # The digest moved with the new fact, so a fingerprint-checked read refuses
    # the text — the freeze path is what decides whether to fall back...
    assert read_overview(conn, "u1", overview_fingerprint(conn, "u1")) is None
    # ...but it is still stored for a human, and reported as stale.
    assert read_overview(conn, "u1") == "总览正文"
    assert overview_status(conn, "u1")["stale"] is True


# ---- the changelog read ------------------------------------------------------


def test_recent_changes_lists_what_happened_newest_first(env):
    conn, project = env
    record_event(
        conn, "fact_written",
        {"type": TYPE_DECISION_RULE, "predicate": "决定", "scope_id": project},
        user_id="u1",
    )
    conn.execute("UPDATE events SET created_at = 2000 WHERE user_id = 'u1'")
    record_event(conn, "facts_archived", {"reason": "capacity"}, user_id="u1")
    conn.execute(
        "UPDATE events SET created_at = 3000 WHERE user_id = 'u1' "
        "AND type = 'facts_archived'"
    )
    conn.commit()

    changes = recent_changes(conn, "u1")
    assert [entry["type"] for entry in changes] == ["facts_archived", "fact_written"]
    assert changes[1]["detail"]["predicate"] == "决定"


def test_recent_changes_skips_lifecycle_noise(env):
    """``task_dead`` and friends are audit, not memory change."""
    conn, _project = env
    record_event(conn, "task_dead", {"task_id": "t1"}, user_id="u1")
    record_event(conn, "index_repaired", {"fixed": {}}, user_id="u1")
    assert recent_changes(conn, "u1") == []


def test_recent_changes_survives_a_malformed_payload(env):
    conn, _project = env
    conn.execute(
        "INSERT INTO events(event_id, user_id, type, payload, created_at) "
        "VALUES ('e1', 'u1', 'fact_written', '{oops', 1000)"
    )
    conn.commit()
    changes = recent_changes(conn, "u1")
    assert len(changes) == 1
    assert changes[0]["detail"] == {}


def test_recent_changes_respects_the_limit(env):
    conn, _project = env
    for index in range(10):
        record_event(conn, "fact_written", {"type": TYPE_SEMANTIC}, user_id="u1")
    assert len(recent_changes(conn, "u1", limit=3)) == 3


def test_recent_changes_is_isolated_per_user(env):
    conn, _project = env
    record_event(conn, "fact_written", {"type": TYPE_SEMANTIC}, user_id="other")
    assert recent_changes(conn, "u1") == []