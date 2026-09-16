"""Tests for the SQLite storage layer (db.py + migrations/001_init.sql)."""

from __future__ import annotations

import pytest

from atom_memory.config import MemConfig
from atom_memory.db import SCHEMA_VERSION, connect_for_tests, open_db
from atom_memory.embedder import serialize_float32


def test_open_db_creates_all_tables(tmp_path):
    """Opening a fresh database must create every expected table."""
    conn = open_db(MemConfig(db_path=str(tmp_path / "test.db")))
    try:
        rows = conn.execute(
            "SELECT name FROM sqlite_master "
            "WHERE type IN ('table','view') AND name NOT LIKE 'sqlite_%' "
            "ORDER BY name"
        ).fetchall()
        names = {r[0] for r in rows}
    finally:
        conn.close()

    expected = {
        "facts",
        "fact_candidates",
        "user_profile",
        "events",
        "task_queue",
        "facts_fts",
        "facts_vec",
        # migration 005: the append-only reuse evidence log.
        "fact_reinforcements",
    }
    assert expected.issubset(names)


def test_in_memory_db_also_migrates():
    """An in-memory connection runs the same migrations."""
    conn = connect_for_tests()
    try:
        version = conn.execute("PRAGMA user_version").fetchone()[0]
        assert version == SCHEMA_VERSION
    finally:
        conn.close()


def test_wal_mode_enabled(tmp_path):
    """The journal must be set to WAL (the connection reports it)."""
    cfg = MemConfig(db_path=str(tmp_path / "test.db"))
    conn = open_db(cfg)
    try:
        mode = conn.execute("PRAGMA journal_mode").fetchone()[0]
        assert mode.lower() == "wal"
    finally:
        conn.close()


def test_vec_extension_loaded():
    """The sqlite-vec extension must expose its version function."""
    conn = connect_for_tests()
    try:
        row = conn.execute("SELECT vec_version()").fetchone()
        assert row is not None and row[0]
    finally:
        conn.close()


def test_facts_vec_is_knn_queryable():
    """facts_vec must accept rows and answer a KNN query."""
    conn = connect_for_tests()
    try:
        conn.execute(
            "INSERT INTO facts_vec(fact_id, embedding) VALUES (?, ?)",
            ("f1", serialize_float32([0.0] * 512)),
        )
        conn.commit()
        rows = conn.execute(
            "SELECT fact_id FROM facts_vec "
            "WHERE embedding MATCH ? ORDER BY distance LIMIT 5",
            (serialize_float32([1.0] * 512),),
        ).fetchall()
        assert rows, "KNN query returned no rows"
        assert rows[0]["fact_id"] == "f1"
    finally:
        conn.close()


def test_facts_fts_is_queryable():
    """facts_fts must accept rows and answer a MATCH query.

    NOTE: FTS5's unicode61 tokenizer treats a contiguous CJK span as a single
    token, so the match must target a whole token. Chinese word segmentation
    (via jieba) is the indexer's responsibility and is exercised elsewhere.
    """
    conn = connect_for_tests()
    try:
        conn.execute(
            "INSERT INTO facts_fts(fact_id, text) VALUES (?, ?)",
            ("f1", "黑咖啡"),
        )
        conn.commit()
        rows = conn.execute(
            "SELECT fact_id FROM facts_fts WHERE facts_fts MATCH ?",
            ("黑咖啡",),
        ).fetchall()
        assert rows, "FTS query returned no rows"
        assert rows[0]["fact_id"] == "f1"
    finally:
        conn.close()


def test_idempotency_key_is_unique(tmp_path):
    """The UNIQUE constraint on fact_candidates.idempotency_key is enforced."""
    conn = open_db(MemConfig(db_path=str(tmp_path / "test.db")))
    try:
        sql = (
            "INSERT INTO fact_candidates(candidate_id, user_id, session_id, "
            "turn_id, idempotency_key, created_at) VALUES (?, ?, ?, ?, ?, ?)"
        )
        conn.execute(sql, ("c1", "u1", "s1", 0, "key-1", 1000))
        conn.commit()
        try:
            conn.execute(sql, ("c2", "u1", "s1", 0, "key-1", 1000))
            conn.commit()
            raised = False
        except Exception:
            conn.rollback()
            raised = True
        assert raised, "UNIQUE(idempotency_key) was not enforced"
    finally:
        conn.close()


def test_all_fact_columns_exist(tmp_path):
    """The facts table must expose every required column."""
    conn = open_db(MemConfig(db_path=str(tmp_path / "test.db")))
    try:
        cols = {
            r[1] for r in conn.execute("PRAGMA table_info(facts)").fetchall()
        }
    finally:
        conn.close()

    expected = {
        "fact_id", "user_id", "session_id", "subject", "predicate", "object",
        "qualifiers", "confidence", "importance", "privacy", "source_type",
        "status", "superseded_by", "observed_at", "created_at", "trace_id",
        "version", "type", "content",
        # migration 005: the reuse-reinforcement aggregate.
        "reinforce_count", "last_used_at", "last_seen_at",
    }
    assert expected.issubset(cols)


def test_all_profile_columns_exist(tmp_path):
    """The user_profile table must expose every required column.

    `pinned` is deliberately absent since v10: it froze a row against automatic
    memory writes, and the profile is no longer written automatically at all.
    """
    conn = open_db(MemConfig(db_path=str(tmp_path / "test.db")))
    try:
        cols = {
            r[1] for r in conn.execute("PRAGMA table_info(user_profile)").fetchall()
        }
    finally:
        conn.close()

    expected = {
        "user_id", "section", "key", "value", "source", "confidence",
        "privacy", "updated_at",
    }
    assert expected.issubset(cols)
    assert "pinned" not in cols


def test_profile_source_records_provenance(tmp_path):
    """`source` distinguishes a typed row from an accepted suggestion."""
    from atom_memory.profile import upsert_profile

    conn = open_db(MemConfig(db_path=str(tmp_path / "test.db")))
    try:
        upsert_profile(conn, "u1", "职业", "value", "工程师")
        upsert_profile(conn, "u1", "城市", "value", "天津", source="generated")
        rows = {
            r["section"]: r["source"]
            for r in conn.execute(
                "SELECT section, source FROM user_profile ORDER BY section"
            ).fetchall()
        }
        assert rows == {"城市": "generated", "职业": "user"}
    finally:
        conn.close()


def test_type_column_defaults_semantic(tmp_path):
    """The new type column defaults to 'semantic' for untouched rows."""
    conn = open_db(MemConfig(db_path=str(tmp_path / "test.db")))
    try:
        conn.execute(
            "INSERT INTO facts(fact_id, user_id, session_id, subject, predicate, "
            "object, observed_at, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            ("f1", "u1", "s1", "用户", "偏好", "黑咖啡", 1000, 1000),
        )
        conn.commit()
        row = conn.execute(
            "SELECT type FROM facts WHERE fact_id = ?", ("f1",)
        ).fetchone()
        assert row["type"] == "semantic"
    finally:
        conn.close()


def test_v1_database_upgrades_to_v2_with_type_default(tmp_path):
    """A database stuck at user_version=1 upgrades to v2 and retro-fits rows.

    Simulates an existing deployment by scripting the v1 schema directly (no
    type column), inserting a row, then reopening through open_db so the
    pending 002 migration adds the column with the default value.
    """
    import sqlite3

    path = str(tmp_path / "legacy.db")
    raw = sqlite3.connect(path)
    raw.executescript(
        """
        CREATE TABLE facts (
            fact_id TEXT PRIMARY KEY, user_id TEXT NOT NULL,
            session_id TEXT NOT NULL, subject TEXT NOT NULL,
            predicate TEXT NOT NULL, object TEXT NOT NULL, qualifiers TEXT,
            confidence REAL NOT NULL DEFAULT 0.5,
            importance REAL NOT NULL DEFAULT 0.5,
            privacy TEXT NOT NULL DEFAULT 'private',
            source_type TEXT NOT NULL DEFAULT 'user_explicit',
            status TEXT NOT NULL DEFAULT 'active', superseded_by TEXT,
            observed_at INTEGER NOT NULL, created_at INTEGER NOT NULL,
            trace_id TEXT, version INTEGER NOT NULL DEFAULT 1
        );
        CREATE TABLE fact_candidates (
            candidate_id TEXT PRIMARY KEY, user_id TEXT NOT NULL,
            session_id TEXT NOT NULL, turn_id INTEGER NOT NULL,
            raw_text TEXT, subject TEXT, predicate TEXT, object TEXT,
            qualifiers TEXT, confidence REAL DEFAULT 0.5,
            importance REAL DEFAULT 0.5, privacy TEXT DEFAULT 'private',
            status TEXT NOT NULL DEFAULT 'pending',
            idempotency_key TEXT UNIQUE, created_at INTEGER NOT NULL
        );
        CREATE TABLE user_profile (
            user_id TEXT NOT NULL, section TEXT NOT NULL, key TEXT NOT NULL,
            value TEXT NOT NULL, source TEXT NOT NULL DEFAULT 'system_inferred',
            confidence REAL NOT NULL DEFAULT 0.5,
            privacy TEXT NOT NULL DEFAULT 'private',
            updated_at INTEGER NOT NULL,
            PRIMARY KEY (user_id, section, key)
        );
        -- A real deployment of this version also has the task queue: migration
        -- 001 creates it and 008 alters it (claim attribution + lease), so a
        -- fixture standing in for a legacy database has to carry it.
        CREATE TABLE task_queue (
            task_id TEXT PRIMARY KEY, task_type TEXT NOT NULL, payload TEXT,
            status TEXT NOT NULL DEFAULT 'pending', priority INTEGER DEFAULT 0,
            retry_count INTEGER DEFAULT 0, max_retries INTEGER DEFAULT 3,
            error TEXT, created_at INTEGER NOT NULL, started_at INTEGER,
            completed_at INTEGER
        );
        PRAGMA user_version = 1;
        """
    )
    raw.execute(
        "INSERT INTO facts(fact_id, user_id, session_id, subject, predicate, "
        "object, observed_at, created_at) VALUES ('f1','u1','s1','用户',"
        "'偏好','黑咖啡',1000,1000)"
    )
    # A genuine v1 database already carries user_profile (migration 001 creates
    # it) — the later `pinned` migration alters exactly this table.
    raw.execute(
        "INSERT INTO user_profile(user_id, section, key, value, updated_at) "
        "VALUES ('u1','职业','value','工程师',1000)"
    )
    raw.commit()
    raw.close()

    conn = open_db(MemConfig(db_path=path))
    try:
        assert conn.execute("PRAGMA user_version").fetchone()[0] == SCHEMA_VERSION
        # v11 clears the legacy facts (scope binding cannot be back-filled; see
        # the migration header), so the retro-fitted columns are exercised on a
        # row written *after* the upgrade — which is what the default really
        # has to serve.
        assert conn.execute(
            "SELECT COUNT(*) AS n FROM facts"
        ).fetchone()["n"] == 0
        conn.execute(
            "INSERT INTO facts(fact_id, user_id, session_id, subject, predicate, "
            "object, observed_at, created_at) VALUES ('f2','u1','s1','用户',"
            "'偏好','黑咖啡',1000,1000)"
        )
        row = conn.execute(
            "SELECT type FROM facts WHERE fact_id = ?", ("f2",)
        ).fetchone()
        assert row is not None
        assert row["type"] == "semantic"
        # v10 clears the old projection output: the row was derived, not
        # curated, and the user's intent was only ever "what the facts implied".
        # The facts are untouched, so it can be re-proposed by 生成画像.
        assert conn.execute(
            "SELECT COUNT(*) AS n FROM user_profile"
        ).fetchone()["n"] == 0
        # The scope dimension exists from this version on, with its root.
        assert conn.execute(
            "SELECT COUNT(*) AS n FROM scope WHERE id = 1 AND path = '/global'"
        ).fetchone()["n"] == 1
    finally:
        conn.close()


def test_v2_database_upgrades_to_v3_with_null_content(tmp_path):
    """A database at user_version=2 upgrades through v3 (NULL content column)
    and v4 (profile `pinned`) in one open."""
    import sqlite3

    path = str(tmp_path / "legacy_v2.db")
    raw = sqlite3.connect(path)
    raw.executescript(
        """
        CREATE TABLE facts (
            fact_id TEXT PRIMARY KEY, user_id TEXT NOT NULL,
            session_id TEXT NOT NULL, subject TEXT NOT NULL,
            predicate TEXT NOT NULL, object TEXT NOT NULL, qualifiers TEXT,
            confidence REAL NOT NULL DEFAULT 0.5,
            importance REAL NOT NULL DEFAULT 0.5,
            privacy TEXT NOT NULL DEFAULT 'private',
            source_type TEXT NOT NULL DEFAULT 'user_explicit',
            status TEXT NOT NULL DEFAULT 'active', superseded_by TEXT,
            observed_at INTEGER NOT NULL, created_at INTEGER NOT NULL,
            trace_id TEXT, version INTEGER NOT NULL DEFAULT 1,
            type TEXT NOT NULL DEFAULT 'semantic'
        );
        CREATE TABLE fact_candidates (
            candidate_id TEXT PRIMARY KEY,
            user_id TEXT NOT NULL,
            session_id TEXT NOT NULL,
            turn_id INTEGER NOT NULL,
            raw_text TEXT,
            subject TEXT,
            predicate TEXT,
            object TEXT,
            qualifiers TEXT,
            confidence REAL DEFAULT 0.5,
            importance REAL DEFAULT 0.5,
            privacy TEXT DEFAULT 'private',
            status TEXT NOT NULL DEFAULT 'pending',
            idempotency_key TEXT UNIQUE,
            created_at INTEGER NOT NULL
        );
        CREATE TABLE user_profile (
            user_id TEXT NOT NULL, section TEXT NOT NULL, key TEXT NOT NULL,
            value TEXT NOT NULL, source TEXT NOT NULL DEFAULT 'system_inferred',
            confidence REAL NOT NULL DEFAULT 0.5,
            privacy TEXT NOT NULL DEFAULT 'private',
            updated_at INTEGER NOT NULL,
            PRIMARY KEY (user_id, section, key)
        );
        -- A real deployment of this version also has the task queue: migration
        -- 001 creates it and 008 alters it (claim attribution + lease), so a
        -- fixture standing in for a legacy database has to carry it.
        CREATE TABLE task_queue (
            task_id TEXT PRIMARY KEY, task_type TEXT NOT NULL, payload TEXT,
            status TEXT NOT NULL DEFAULT 'pending', priority INTEGER DEFAULT 0,
            retry_count INTEGER DEFAULT 0, max_retries INTEGER DEFAULT 3,
            error TEXT, created_at INTEGER NOT NULL, started_at INTEGER,
            completed_at INTEGER
        );
        PRAGMA user_version = 2;
        """
    )
    raw.execute(
        "INSERT INTO facts(fact_id, user_id, session_id, subject, predicate, "
        "object, observed_at, created_at) VALUES ('f1','u1','s1','用户',"
        "'偏好','黑咖啡',1000,1000)"
    )
    raw.execute(
        "INSERT INTO user_profile(user_id, section, key, value, updated_at) "
        "VALUES ('u1','职业','value','工程师',1000)"
    )
    raw.commit()
    raw.close()

    conn = open_db(MemConfig(db_path=path))
    try:
        assert conn.execute("PRAGMA user_version").fetchone()[0] == SCHEMA_VERSION
        # v11 clears the legacy facts (see its migration header), so the
        # retro-fitted `type` / `content` columns are checked on a row written
        # after the upgrade.
        assert conn.execute(
            "SELECT COUNT(*) AS n FROM facts"
        ).fetchone()["n"] == 0
        conn.execute(
            "INSERT INTO facts(fact_id, user_id, session_id, subject, predicate, "
            "object, observed_at, created_at) VALUES ('f2','u1','s1','用户',"
            "'偏好','黑咖啡',1000,1000)"
        )
        row = conn.execute(
            "SELECT type, content FROM facts WHERE fact_id = ?", ("f2",)
        ).fetchone()
        # pre-existing rows get type default and NULL content
        assert row["type"] == "semantic"
        assert row["content"] is None
        # ...and the old projection output is cleared by v10 (the surviving
        # facts are what matter; see the note in the v1 test above).
        assert conn.execute(
            "SELECT COUNT(*) AS n FROM user_profile"
        ).fetchone()["n"] == 0
        # column accepts a structured body
        conn.execute(
            "UPDATE facts SET content = ? WHERE fact_id = ?",
            ('{"steps":["a","b"]}', "f2"),
        )
        conn.commit()
        assert conn.execute(
            "SELECT content FROM facts WHERE fact_id = ?", ("f2",)
        ).fetchone()["content"] == '{"steps":["a","b"]}'
    finally:
        conn.close()


def test_v3_database_upgrades_through_the_profile_ownership_migration(tmp_path):
    """A database at user_version=3 opens at the current schema.

    Migration 004 retro-fits `pinned` for rows that predate it, and migration
    010 then removes that column again and clears the projection output — the
    profile became a table the user owns, so those rows (which the old
    projection wrote, not the user) are dropped while the facts they came from
    stay. This test drives the whole chain in one open, which is what an
    existing deployment actually experiences.
    """
    import sqlite3

    path = str(tmp_path / "legacy_v3.db")
    raw = sqlite3.connect(path)
    raw.executescript(
        """
        CREATE TABLE facts (
            fact_id TEXT PRIMARY KEY, user_id TEXT NOT NULL,
            session_id TEXT NOT NULL, subject TEXT NOT NULL,
            predicate TEXT NOT NULL, object TEXT NOT NULL, qualifiers TEXT,
            confidence REAL NOT NULL DEFAULT 0.5,
            importance REAL NOT NULL DEFAULT 0.5,
            privacy TEXT NOT NULL DEFAULT 'private',
            source_type TEXT NOT NULL DEFAULT 'user_explicit',
            status TEXT NOT NULL DEFAULT 'active', superseded_by TEXT,
            observed_at INTEGER NOT NULL, created_at INTEGER NOT NULL,
            trace_id TEXT, version INTEGER NOT NULL DEFAULT 1,
            type TEXT NOT NULL DEFAULT 'semantic', content TEXT
        );
        INSERT INTO facts(fact_id, user_id, session_id, subject, predicate,
            object, observed_at, created_at)
            VALUES ('f1','u1','s1','用户','偏好','黑咖啡',1000,1000);
        CREATE TABLE user_profile (
            user_id TEXT NOT NULL, section TEXT NOT NULL, key TEXT NOT NULL,
            value TEXT NOT NULL, source TEXT NOT NULL DEFAULT 'system_inferred',
            confidence REAL NOT NULL DEFAULT 0.5,
            privacy TEXT NOT NULL DEFAULT 'private',
            updated_at INTEGER NOT NULL,
            PRIMARY KEY (user_id, section, key)
        );
        INSERT INTO user_profile(user_id, section, key, value, updated_at)
            VALUES ('u1','职业','value','工程师',1000);
        -- A real deployment also has the candidate table: migration 001 creates
        -- it and 002/003/007 alter it, so a fixture without it is not a schema a
        -- v3 database could actually be in.
        CREATE TABLE fact_candidates (
            candidate_id TEXT PRIMARY KEY, user_id TEXT NOT NULL,
            session_id TEXT NOT NULL, turn_id INTEGER NOT NULL,
            raw_text TEXT, subject TEXT, predicate TEXT, object TEXT,
            qualifiers TEXT, confidence REAL DEFAULT 0.5,
            importance REAL DEFAULT 0.5, privacy TEXT DEFAULT 'private',
            status TEXT NOT NULL DEFAULT 'pending',
            idempotency_key TEXT UNIQUE, created_at INTEGER NOT NULL,
            type TEXT NOT NULL DEFAULT 'semantic', content TEXT
        );
        -- A real deployment of this version also has the task queue: migration
        -- 001 creates it and 008 alters it (claim attribution + lease), so a
        -- fixture standing in for a legacy database has to carry it.
        CREATE TABLE task_queue (
            task_id TEXT PRIMARY KEY, task_type TEXT NOT NULL, payload TEXT,
            status TEXT NOT NULL DEFAULT 'pending', priority INTEGER DEFAULT 0,
            retry_count INTEGER DEFAULT 0, max_retries INTEGER DEFAULT 3,
            error TEXT, created_at INTEGER NOT NULL, started_at INTEGER,
            completed_at INTEGER
        );
        PRAGMA user_version = 3;
        """
    )
    raw.commit()
    raw.close()

    conn = open_db(MemConfig(db_path=path))
    try:
        assert conn.execute("PRAGMA user_version").fetchone()[0] == SCHEMA_VERSION
        # v10 leaves no `pinned` column behind, and clears the projection
        # output. v11 then clears the facts themselves — scope binding cannot be
        # back-filled (see the migration header), so the memory store starts
        # empty rather than with rows filed under a scope nobody chose.
        cols = {r[1] for r in conn.execute("PRAGMA table_info(user_profile)").fetchall()}
        assert "pinned" not in cols
        assert conn.execute(
            "SELECT COUNT(*) AS n FROM user_profile"
        ).fetchone()["n"] == 0
        assert conn.execute(
            "SELECT COUNT(*) AS n FROM facts"
        ).fetchone()["n"] == 0
        # The scope tree is in place and indexed, and the index tables accept
        # writes (they were re-created by v11).
        assert conn.execute(
            "SELECT COUNT(*) AS n FROM scope"
        ).fetchone()["n"] == 1
        conn.execute(
            "INSERT INTO facts_fts(fact_id, text) VALUES ('x', 'text')"
        )
        assert conn.execute(
            "SELECT COUNT(*) AS n FROM facts_fts"
        ).fetchone()["n"] == 1
    finally:
        conn.close()


def test_v4_database_upgrades_to_v5_with_zero_reinforcement(tmp_path):
    """A database at user_version=4 gains the reinforcement columns.

    Migration 005 must retro-fit existing facts as *un-reinforced*: their
    effective importance stays exactly the importance written at extraction
    time, so upgrading never silently re-ranks an existing memory.

    Migration 011 then clears those facts (see its header: scope binding cannot
    be back-filled), so the retro-fitted defaults are asserted on a row written
    after the upgrade — which is the row the defaults actually have to serve from
    this version on.
    """
    import sqlite3

    path = str(tmp_path / "legacy_v4.db")
    raw = sqlite3.connect(path)
    raw.executescript(
        """
        CREATE TABLE facts (
            fact_id TEXT PRIMARY KEY, user_id TEXT NOT NULL,
            session_id TEXT NOT NULL, subject TEXT NOT NULL,
            predicate TEXT NOT NULL, object TEXT NOT NULL, qualifiers TEXT,
            confidence REAL NOT NULL DEFAULT 0.5,
            importance REAL NOT NULL DEFAULT 0.5,
            privacy TEXT NOT NULL DEFAULT 'private',
            source_type TEXT NOT NULL DEFAULT 'user_explicit',
            status TEXT NOT NULL DEFAULT 'active', superseded_by TEXT,
            observed_at INTEGER NOT NULL, created_at INTEGER NOT NULL,
            trace_id TEXT, version INTEGER NOT NULL DEFAULT 1,
            type TEXT NOT NULL DEFAULT 'semantic', content TEXT
        );
        INSERT INTO facts(fact_id, user_id, session_id, subject, predicate,
            object, importance, observed_at, created_at)
            VALUES ('f1','u1','s1','用户','偏好','黑咖啡',0.75,1000,1000);
        CREATE TABLE user_profile (
            user_id TEXT NOT NULL, section TEXT NOT NULL, key TEXT NOT NULL,
            value TEXT NOT NULL, source TEXT NOT NULL DEFAULT 'system_inferred',
            confidence REAL NOT NULL DEFAULT 0.5,
            privacy TEXT NOT NULL DEFAULT 'private',
            updated_at INTEGER NOT NULL,
            pinned INTEGER NOT NULL DEFAULT 0,
            PRIMARY KEY (user_id, section, key)
        );
        -- Present in any real v3/v4 deployment: 001 creates it, 002/003 alter
        -- it, and 007 adds the write-outcome columns to it.
        CREATE TABLE fact_candidates (
            candidate_id TEXT PRIMARY KEY, user_id TEXT NOT NULL,
            session_id TEXT NOT NULL, turn_id INTEGER NOT NULL,
            raw_text TEXT, subject TEXT, predicate TEXT, object TEXT,
            qualifiers TEXT, confidence REAL DEFAULT 0.5,
            importance REAL DEFAULT 0.5, privacy TEXT DEFAULT 'private',
            status TEXT NOT NULL DEFAULT 'pending',
            idempotency_key TEXT UNIQUE, created_at INTEGER NOT NULL,
            type TEXT NOT NULL DEFAULT 'semantic', content TEXT
        );
        -- A real deployment of this version also has the task queue: migration
        -- 001 creates it and 008 alters it (claim attribution + lease), so a
        -- fixture standing in for a legacy database has to carry it.
        CREATE TABLE task_queue (
            task_id TEXT PRIMARY KEY, task_type TEXT NOT NULL, payload TEXT,
            status TEXT NOT NULL DEFAULT 'pending', priority INTEGER DEFAULT 0,
            retry_count INTEGER DEFAULT 0, max_retries INTEGER DEFAULT 3,
            error TEXT, created_at INTEGER NOT NULL, started_at INTEGER,
            completed_at INTEGER
        );
        PRAGMA user_version = 4;
        """
    )
    raw.commit()
    raw.close()

    conn = open_db(MemConfig(db_path=path))
    try:
        assert conn.execute("PRAGMA user_version").fetchone()[0] == SCHEMA_VERSION
        conn.execute(
            "INSERT INTO facts(fact_id, user_id, session_id, subject, predicate, "
            "object, importance, observed_at, created_at) "
            "VALUES ('f2','u1','s1','用户','偏好','黑咖啡',0.75,1000,1000)"
        )
        row = conn.execute(
            "SELECT importance, reinforce_count, last_used_at, last_seen_at "
            "FROM facts WHERE fact_id = 'f2'"
        ).fetchone()
        assert row["importance"] == pytest.approx(0.75)
        assert row["reinforce_count"] == 0.0
        assert row["last_used_at"] is None
        assert row["last_seen_at"] is None
        # The evidence log exists and starts empty.
        assert conn.execute(
            "SELECT COUNT(*) AS n FROM fact_reinforcements"
        ).fetchone()["n"] == 0
    finally:
        conn.close()

