"""Tests for the retrieval pipeline (retriever.py): FTS + vector + RRF +
re-ranking + token budgeting, together with user-scoped recall semantics."""

from __future__ import annotations

import asyncio

import pytest

from atom_memory.config import MemConfig
from atom_memory.db import connect_for_tests
from atom_memory.embedder import serialize_float32
from atom_memory.retriever import (
    Retriever,
    SOURCE_CREDIBILITY,
    estimate_tokens,
    relevance_from_rrf,
    rrf_ceiling,
    rrf_merge,
    segment_text,
)

DIM = 512


def _vec(fill: float) -> bytes:
    return serialize_float32([fill] * DIM)


def _insert_fact(
    conn,
    fact_id: str,
    user_id: str,
    subject: str,
    predicate: str,
    obj: str,
    status: str = "active",
    source_type: str = "user_explicit",
    importance: float = 0.6,
):
    conn.execute(
        "INSERT INTO facts(fact_id, user_id, session_id, subject, predicate, "
        "object, confidence, importance, source_type, status, observed_at, "
        "created_at, version) VALUES (?, ?, ?, ?, ?, ?, 0.8, ?, ?, ?, 1, 2, 1)",
        (
            fact_id, user_id, "s_test", subject, predicate, obj,
            importance, source_type, status,
        ),
    )
    conn.execute(
        "INSERT INTO facts_fts(fact_id, text) VALUES (?, ?)",
        (fact_id, " ".join(segment_text(f"{subject} {predicate} {obj}"))),
    )
    conn.execute(
        "INSERT INTO facts_vec(fact_id, embedding) VALUES (?, ?)",
        (fact_id, _vec(1.0)),
    )


class _FakeEmbed:
    """A fake embedder: every query gets the same vector, so vector KNN picks
    up facts whose vectors we inserted with the same value."""

    def __init__(self) -> None:
        self.calls = 0

    def embed_one(self, text: str) -> bytes:
        self.calls += 1
        return _vec(1.0)


# ---- RRF --------------------------------------------------------------------

def test_rrf_merge_ranks_overlap_higher():
    fts = ["a", "b", "c"]
    vec = ["b", "d", "e"]
    merged = rrf_merge(fts, vec, k=60)
    ids = [x[0] for x in merged]
    # "b" appears in both lists, so it should rank first.
    assert ids[0] == "b"
    # scores strictly descending
    scores = [s for _, s in merged]
    assert scores == sorted(scores, reverse=True)


def test_rrf_merge_preserves_single_list_order():
    merged = rrf_merge(["x", "y"], [], k=60)
    assert [x[0] for x in merged] == ["x", "y"]


def test_source_credibility_table():
    assert SOURCE_CREDIBILITY["user_explicit"] == 1.00
    assert SOURCE_CREDIBILITY["user_confirmed"] == 0.95
    assert SOURCE_CREDIBILITY["model_generated"] == 0.30


# ---- token estimate ----------------------------------------------------------

def test_estimate_tokens_basic():
    assert estimate_tokens("") == 0
    assert estimate_tokens("你好世界") == 4  # 4 CJK chars
    assert estimate_tokens("hello") >= 1


# ---- retrieval primitives (vector / fts) -------------------------------------

def test_vector_knn_filters_user_and_status():
    conn = connect_for_tests()
    try:
        _insert_fact(conn, "f1", "u1", "用户", "偏好", "黑咖啡")
        _insert_fact(conn, "f2", "u1", "用户", "偏好", "加糖")
        _insert_fact(conn, "f3", "u1", "用户", "偏好", "奶茶", status="retracted")
        _insert_fact(conn, "f4", "u2", "用户", "偏好", "其他用户的")
        conn.commit()

        r = Retriever(conn, _FakeEmbed().embed_one)
        ids = r._vector_knn("u1", _vec(1.0), 10)
        assert set(ids) == {"f1", "f2"}  # only active u1 facts
    finally:
        conn.close()


def test_fts_search_returns_row():
    conn = connect_for_tests()
    try:
        _insert_fact(conn, "f1", "u1", "用户", "偏好", "黑咖啡")
        conn.commit()
        r = Retriever(conn, _FakeEmbed().embed_one)
        ids = r._fts_search("u1", "黑咖啡", 10)
        assert "f1" in ids
    finally:
        conn.close()


# ---- full search pipeline ------------------------------------------------------

def _search(conn, user_id, query, embed, **kw):
    retriever = Retriever(conn, embed.embed_one)
    return asyncio.run(retriever.search(user_id, query, **kw))


def test_search_returns_ranked_facts_with_final_score():
    conn = connect_for_tests()
    try:
        _insert_fact(conn, "f1", "u1", "用户", "偏好", "黑咖啡")
        _insert_fact(conn, "f2", "u1", "用户", "偏好", "加糖")
        conn.commit()
        embed = _FakeEmbed()
        facts = _search(conn, "u1", "咖啡", embed, top_k=10)
        assert len(facts) == 2
        ids = {f["fact_id"] for f in facts}
        assert ids == {"f1", "f2"}
        for f in facts:
            assert "final_score" in f
            assert f["status"] == "active"
        # sorted descending by final_score
        scores = [f["final_score"] for f in facts]
        assert scores == sorted(scores, reverse=True)
    finally:
        conn.close()


def test_search_isolates_user():
    conn = connect_for_tests()
    try:
        _insert_fact(conn, "f1", "u1", "用户", "偏好", "黑咖啡")
        _insert_fact(conn, "f2", "u2", "用户", "偏好", "黑咖啡")
        conn.commit()
        embed = _FakeEmbed()
        facts = _search(conn, "u1", "咖啡", embed, top_k=10)
        assert [f["fact_id"] for f in facts] == ["f1"]
    finally:
        conn.close()


def test_search_excludes_inactive():
    conn = connect_for_tests()
    try:
        _insert_fact(conn, "f1", "u1", "用户", "偏好", "黑咖啡")
        _insert_fact(conn, "f2", "u1", "用户", "偏好", "旧奶茶", status="superseded")
        conn.commit()
        embed = _FakeEmbed()
        facts = _search(conn, "u1", "咖啡", embed, top_k=10)
        assert [f["fact_id"] for f in facts] == ["f1"]
    finally:
        conn.close()


def test_search_empty_query_returns_empty():
    conn = connect_for_tests()
    try:
        embed = _FakeEmbed()
        facts = _search(conn, "u1", "   ", embed, top_k=10)
        assert facts == []
    finally:
        conn.close()


# ---- rerank formula -----------------------------------------------------------

def test_rerank_ties_break_by_confidence():
    conn = connect_for_tests()
    try:
        # Two facts with identical importance/recency; higher-confidence one
        # should win via the trust term.
        _insert_fact(conn, "f_high", "u1", "用户", "偏好", "黑咖啡")
        conn.execute(
            "UPDATE facts SET confidence = 0.99 WHERE fact_id = 'f_high'"
        )
        _insert_fact(conn, "f_low", "u1", "用户", "偏好", "加糖")
        conn.execute(
            "UPDATE facts SET confidence = 0.4 WHERE fact_id = 'f_low'"
        )
        conn.commit()
        embed = _FakeEmbed()
        facts = _search(conn, "u1", "咖啡", embed, top_k=10)
        assert facts[0]["fact_id"] == "f_high"
    finally:
        conn.close()


# ---- summary / profile rendering (derived views) -----------------------------

def test_summary_contains_fact_id():
    from atom_memory.summary import generate_summary

    conn = connect_for_tests()
    try:
        _insert_fact(conn, "f1", "u1", "用户", "偏好", "黑咖啡")
        conn.commit()
        md = generate_summary(conn, "u1", max_tokens=2000, detail=True)
        assert "f1" in md
        assert "黑咖啡" in md
        # other user not shown
        md2 = generate_summary(conn, "u2", max_tokens=2000, detail=True)
        assert "暂无" in md2
    finally:
        conn.close()


def test_summary_compact_omits_fact_id():
    """The injected depth drops the UUIDs, keeping the content itself."""
    from atom_memory.summary import generate_summary

    conn = connect_for_tests()
    try:
        _insert_fact(conn, "f1", "u1", "用户", "偏好", "黑咖啡")
        conn.commit()
        md = generate_summary(conn, "u1", max_tokens=2000, detail=False)
        assert "黑咖啡" in md
        assert "f1" not in md
    finally:
        conn.close()


def test_upsert_profile_source_priority():
    from atom_memory.profile import profile_md, upsert_profile

    conn = connect_for_tests()
    try:
        # weak source first, then strong source -> strong wins
        ok1 = upsert_profile(
            conn, "u1", "职业", "value", "工程师",
            source="system_inferred_low", confidence=0.5,
        )
        ok2 = upsert_profile(
            conn, "u1", "职业", "value", "科学家",
            source="user_explicit", confidence=0.9,
        )
        assert ok1 and ok2
        row = conn.execute(
            "SELECT value FROM user_profile WHERE user_id='u1' AND section='职业'"
        ).fetchone()
        assert row["value"] == "科学家"
        # strong source already present -> weaker source must not downgrade
        ok3 = upsert_profile(
            conn, "u1", "职业", "value", "教师",
            source="model_generated", confidence=0.3,
        )
        assert ok3 is False
        row = conn.execute(
            "SELECT value FROM user_profile WHERE user_id='u1' AND section='职业'"
        ).fetchone()
        assert row["value"] == "科学家"  # unchanged
        md = profile_md(conn, "u1", max_tokens=2000)
        assert "科学家" in md
    finally:
        conn.close()


def test_pinned_profile_row_blocks_automatic_writes():
    """A pinned row is frozen: derived writes may not update or replace it.

    Only a write that carries the pin explicitly (the settings panel toggling
    it) gets through, which is what makes the flag releasable at all.
    """
    from atom_memory.profile import upsert_profile

    conn = connect_for_tests()
    try:
        assert upsert_profile(
            conn, "u1", "职业", "value", "工程师",
            source="user_explicit", confidence=0.9,
        )
        # The panel's write path: value plus the pin itself.
        assert upsert_profile(
            conn, "u1", "职业", "value", "工程师",
            source="user_explicit", confidence=0.9, pinned=True,
        )
        assert conn.execute(
            "SELECT pinned FROM user_profile WHERE user_id='u1'"
        ).fetchone()["pinned"] == 1

        # A derived write of equal (or higher) authority would normally win —
        # the pin is what stops it, not the source ranking.
        assert upsert_profile(
            conn, "u1", "职业", "value", "产品经理",
            source="user_explicit", confidence=0.9,
        ) is False
        assert conn.execute(
            "SELECT value FROM user_profile WHERE user_id='u1'"
        ).fetchone()["value"] == "工程师"

        # Unpinning releases the row back to the normal rules.
        assert upsert_profile(
            conn, "u1", "职业", "value", "工程师",
            source="user_explicit", confidence=0.9, pinned=False,
        )
        assert upsert_profile(
            conn, "u1", "职业", "value", "产品经理",
            source="user_explicit", confidence=0.9,
        )
        row = conn.execute(
            "SELECT value, pinned FROM user_profile WHERE user_id='u1'"
        ).fetchone()
        assert row["value"] == "产品经理"
        assert row["pinned"] == 0
    finally:
        conn.close()


def test_derive_profile_from_facts_single_and_multi():
    from atom_memory.profile import derive_profile_from_facts

    conn = connect_for_tests()
    try:
        _insert_fact(conn, "f1", "u1", "用户", "偏好", "黑咖啡")
        _insert_fact(conn, "f2", "u1", "用户", "职业", "工程师")
        conn.commit()
        n = derive_profile_from_facts(conn, "u1")
        assert n >= 2
        prefs = conn.execute(
            "SELECT section, key, value FROM user_profile "
            "WHERE user_id='u1' AND section='偏好'"
        ).fetchall()
        assert any(r["key"] == "黑咖啡" and r["value"] == "喜欢" for r in prefs)
        prof = conn.execute(
            "SELECT value FROM user_profile WHERE user_id='u1' AND section='职业'"
        ).fetchone()
        assert prof["value"] == "工程师"
    finally:
        conn.close()


# ---- relevance is absolute, and the gates can say "nothing relevant" ----------

def _retriever(conn, config=None, embed=None):
    embed = embed or _FakeEmbed()
    return Retriever(
        conn, embed.embed_one, top_k_default=10, config=config
    )


def test_relevance_is_absolute_not_rescaled_per_query():
    """A lone mediocre hit must not score like a top-of-both-lists hit.

    Min-max normalisation over the candidate set gave the best candidate 1.0
    *whatever* it was, which made a single weak match indistinguishable from a
    perfect one. The absolute scale maps a one-list hit to ~0.5 and a
    both-lists hit to 1.0.
    """
    assert relevance_from_rrf(rrf_ceiling(60), 60) == pytest.approx(1.0)
    assert relevance_from_rrf(1.0 / 61, 60) == pytest.approx(0.5)
    # A weaker rank on one list is worth less than a top rank on both.
    assert relevance_from_rrf(1.0 / 70, 60) < relevance_from_rrf(2.0 / 61, 60)


def test_a_single_candidate_no_longer_scores_full_relevance():
    """Relevance is computed from the fusion, not forced to 1.0 by normalisation.

    Unit-level on purpose: the candidate set here would have been min-max
    normalised to 1.0 before, whatever the match quality was.
    """
    retriever = Retriever.__new__(Retriever)
    retriever.config = MemConfig()
    fact = {
        "fact_id": "f1", "subject": "用户", "predicate": "偏好", "object": "黑咖啡",
        "confidence": 0.8, "importance": 0.6, "source_type": "user_explicit",
        "status": "active", "created_at": 2, "type": "semantic", "content": None,
        "reinforce_count": 0.0, "last_used_at": None, "age_at": 2,
    }
    # A top hit in one list only: half the ceiling.
    one_list = retriever._rerank([dict(fact)], {"f1": 1.0 / 61})
    assert one_list[0]["relevance"] == pytest.approx(0.5)
    # A top hit in both lists: the ceiling.
    both = retriever._rerank([dict(fact)], {"f1": 2.0 / 61})
    assert both[0]["relevance"] == pytest.approx(1.0)
    assert both[0]["final_score"] < 1.0


def test_min_relevance_makes_nothing_relevant_a_valid_answer():
    """A lexical-only match scores half the ceiling, so a floor can refuse it.

    Built with 11 near neighbours so the one lexical-only fact really is outside
    the vector top-k: that is the situation a rank-based floor cannot detect and
    an absolute one can.
    """
    conn = connect_for_tests()
    try:
        for index in range(11):
            _insert_fact(conn, f"near{index}", "u1", "用户", "无关", f"内容{index}")
        conn.execute(
            "INSERT INTO facts(fact_id, user_id, session_id, subject, predicate, "
            "object, confidence, importance, source_type, status, observed_at, "
            "created_at, version, type) VALUES ('lexical','u1','s','用户','偏好',"
            "'黑咖啡',0.8,0.6,'user_explicit','active',1,2,1,'semantic')"
        )
        conn.execute(
            "INSERT INTO facts_fts(fact_id, text) VALUES ('lexical', ?)",
            (" ".join(segment_text("用户 偏好 黑咖啡")),),
        )
        conn.execute(
            "INSERT INTO facts_vec(fact_id, embedding) VALUES ('lexical', ?)",
            (serialize_float32([-1.0] * DIM),),  # farthest from the query vector
        )
        conn.commit()

        baseline = _search(conn, "u1", "咖啡", _FakeEmbed(), top_k=10)
        lexical = [f for f in baseline if f["fact_id"] == "lexical"]
        assert lexical, "the lexical hit is still returned"
        rel = lexical[0]["relevance"]
        assert rel < 1.0, "a lexical-only match does not score full relevance"

        # A floor just under it keeps it; a floor just over it refuses it, so the
        # caller gets an empty answer instead of the least-bad row.
        loose = Retriever(
            conn, _FakeEmbed().embed_one,
            config=MemConfig(min_relevance=rel - 0.01),
        )
        assert any(
            f["fact_id"] == "lexical"
            for f in asyncio.run(loose.search("u1", "咖啡", top_k=10))
        )
        strict = Retriever(
            conn, _FakeEmbed().embed_one,
            config=MemConfig(min_relevance=rel + 0.01),
        )
        kept = {
            f["fact_id"]
            for f in asyncio.run(strict.search("u1", "咖啡", top_k=10))
        }
        assert "lexical" not in kept
    finally:
        conn.close()


def test_vector_distance_gate_filters_far_rows():
    conn = connect_for_tests()
    try:
        _insert_fact(conn, "near", "u1", "用户", "偏好", "黑咖啡")
        # A distant vector: same table, opposite direction.
        conn.execute(
            "INSERT INTO facts(fact_id, user_id, session_id, subject, predicate, "
            "object, confidence, importance, source_type, status, observed_at, "
            "created_at, version, type) VALUES ('far','u1','s','用户','偏好',"
            "'无关内容',0.8,0.6,'user_explicit','active',1,2,1,'semantic')"
        )
        conn.execute(
            "INSERT INTO facts_fts(fact_id, text) VALUES ('far', ?)",
            (" ".join(segment_text("用户 偏好 无关内容")),),
        )
        conn.execute(
            "INSERT INTO facts_vec(fact_id, embedding) VALUES ('far', ?)",
            (serialize_float32([-1.0] * DIM),),
        )
        conn.commit()

        open_gate = Retriever(conn, _FakeEmbed().embed_one)
        assert {f["fact_id"] for f in asyncio.run(
            open_gate.search("u1", "咖啡", top_k=10)
        )} == {"near", "far"}

        gated = Retriever(
            conn, _FakeEmbed().embed_one, config=MemConfig(max_vector_distance=1.0)
        )
        gated_facts = asyncio.run(gated.search("u1", "咖啡", top_k=10))
        # 'far' is still a lexical hit, so what the gate removes is its *semantic*
        # support: it survives only through FTS.
        assert all(f["fact_id"] != "far" or f["relevance"] < 1.0 for f in gated_facts)
        direct = gated._vector_knn("u1", _vec(1.0), 10, max_distance=1.0)
        assert direct == ["near"]
    finally:
        conn.close()


def test_a_broken_index_is_reported_rather_than_looking_empty():
    conn = connect_for_tests()
    try:
        _insert_fact(conn, "f1", "u1", "用户", "偏好", "黑咖啡")
        conn.commit()
        conn.execute("DROP TABLE facts_fts")
        conn.commit()
        r = Retriever(conn, _FakeEmbed().embed_one)
        assert r._fts_search("u1", "咖啡", 10) == []
        assert r.last_degraded == ["fts"]
        # The vector half still answers, so recall is degraded, not dead.
        facts = asyncio.run(r.search("u1", "咖啡", top_k=10))
        assert r.last_degraded == ["fts"]
        assert [f["fact_id"] for f in facts] == ["f1"]
    finally:
        conn.close()


def test_weights_are_configurable():
    conn = connect_for_tests()
    try:
        _insert_fact(conn, "old", "u1", "用户", "偏好", "黑咖啡")
        conn.execute("UPDATE facts SET created_at = 1 WHERE fact_id = 'old'")
        _insert_fact(conn, "new", "u1", "用户", "偏好", "绿茶")
        conn.execute("UPDATE facts SET created_at = 999999999999 WHERE fact_id = 'new'")
        conn.commit()
        from atom_memory.db import now_ms

        recency_only = MemConfig(
            w_rrf=0.0, w_importance=0.0, w_recency=1.0, w_trust=0.0
        )
        facts = asyncio.run(
            Retriever(
                conn, _FakeEmbed().embed_one, config=recency_only
            ).search("u1", "咖啡", top_k=10)
        )
        assert facts[0]["fact_id"] == "new", "recency-only ranks the newest first"
        assert now_ms() > 0
    finally:
        conn.close()

