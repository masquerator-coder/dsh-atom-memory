-- dsh-atom-memory schema, migration 011 (scope-aware memory).
-- Executed by dsh_atom_memory.db.open_db() via PRAGMA user_version gating.
--
-- Until now "scope" was an implicit semantic property: every fact shared one
-- global pool, so project A's experience was recalled for project B, a global
-- rule could not be told apart from a project-specific one, and two similar
-- claims from different projects were merged or judged contradictory. This
-- migration makes scope an explicit storage dimension:
--
--   scope            one node per (type, name) in a hierarchy, with a
--                    materialised `path` (e.g. /global/org:acme/project:api)
--   scope_alias      extra names one scope answers to (repo name, doc title...)
--   scope_signal     the *fingerprints* that identify a scope (git remote,
--                    git root, package name, folder path...). UNIQUE per
--                    (signal_type, normalized_value): a signal identifies at
--                    most one scope, which is what makes resolution decidable.
--   scope_candidate  the low-confidence queue. A candidate that never earns a
--                    high-confidence signal is NOT promoted to a scope on
--                    first sight (a wrong scope pollutes recall); it is counted
--                    here and only promoted after repeated, consistent
--                    evidence (see MemConfig.scope_promote_after).
--   fact_scope       facts <-> scopes (many-to-many, `priority` breaks ties)
--   fact_condition   "cross-project but conditional" experience, e.g.
--                    doc_type=proposal / language=typescript / stage=final
--   fact_origin      provenance of an abstraction: which concrete facts a
--                    promoted global rule was derived from
--   fact_evolution   how one fact evolved into another in the *same* scope
--                    (evolves_to / supersedes / exception / abstraction), so a
--                    phase change keeps both statements instead of overwriting
--
-- A fact with **no** `fact_scope` row means "global": that keeps every row
-- written before this migration meaningful without inventing a binding, and
-- keeps the read paths total (an unscoped fact is always recallable). The
-- resolution code still writes an explicit `/global` binding for anything it
-- decides is global, so "unscoped" is a compatibility case, not a normal one.
--
-- `scope.id = 1` is reserved for the root. It is inserted here rather than
-- looked up by name so the ancestor walk can hard-code the root id, and so
-- `path` prefixes are stable from the first write.
--
-- ---------------------------------------------------------------------------
-- This migration CLEARS the fact store.
-- ---------------------------------------------------------------------------
-- Scope binding cannot be back-filled: nothing in an existing row says which
-- project, client or phase it came from, and guessing (binding everything to
-- global) would silently re-create exactly the cross-project pool this change
-- exists to remove. The deployment this ships to opted for a clean start, so
-- the fact tables are emptied here: facts, their FTS/vector entries, their
-- reinforcement log and their candidate provenance. `user_profile` is NOT
-- touched — since migration 010 it is a table the user owns and it is not
-- derived from facts. Future migrations must never repeat this: it is a
-- one-time consequence of introducing the dimension, not a policy.
--
-- The two search indexes are dropped and re-created rather than emptied with
-- DELETE: `DROP TABLE IF EXISTS` is the only form that is safe on a database
-- whose indexes never existed (a hand-built fixture, a deployment whose
-- migration 001 was applied by an older tool), and re-creating them from the
-- same definition migration 001 uses guarantees the rewritten index is not
-- merely empty but structurally current.

CREATE TABLE scope (
    id             INTEGER PRIMARY KEY,
    scope_type     TEXT NOT NULL,
    canonical_name TEXT NOT NULL,
    parent_id      INTEGER REFERENCES scope(id),
    path           TEXT NOT NULL,
    status         TEXT NOT NULL DEFAULT 'active',
    merged_into    INTEGER REFERENCES scope(id),
    confidence     REAL NOT NULL DEFAULT 0.5,
    created_at     INTEGER NOT NULL,
    last_seen_at   INTEGER NOT NULL,
    CHECK (scope_type IN ('global','user','org','team','client','project',
                          'series','phase','document','thread')),
    CHECK (status IN ('active','merged','archived'))
);
CREATE INDEX idx_scope_parent ON scope(parent_id);
CREATE INDEX idx_scope_path   ON scope(path);
CREATE INDEX idx_scope_type   ON scope(scope_type, status);
-- One node per (parent, type, name): two resolutions of the same signal must
-- land on the same row instead of racing two rows into existence.
CREATE UNIQUE INDEX idx_scope_unique ON scope(parent_id, scope_type, canonical_name);
-- `parent_id IS NULL` is unique per name too: SQLite treats NULLs as distinct
-- in a UNIQUE index, so the root would otherwise be insertable twice.
CREATE UNIQUE INDEX idx_scope_root ON scope(canonical_name) WHERE parent_id IS NULL;

CREATE TABLE scope_alias (
    id         INTEGER PRIMARY KEY,
    scope_id   INTEGER NOT NULL REFERENCES scope(id) ON DELETE CASCADE,
    alias      TEXT NOT NULL,
    alias_type TEXT NOT NULL,
    confidence REAL NOT NULL DEFAULT 0.5,
    first_seen INTEGER NOT NULL,
    last_seen  INTEGER NOT NULL
);
CREATE UNIQUE INDEX idx_alias_unique ON scope_alias(alias_type, alias);
CREATE INDEX idx_alias_scope ON scope_alias(scope_id);

CREATE TABLE scope_signal (
    id               INTEGER PRIMARY KEY,
    scope_id         INTEGER NOT NULL REFERENCES scope(id) ON DELETE CASCADE,
    signal_type      TEXT NOT NULL,
    signal_value     TEXT NOT NULL,
    normalized_value TEXT NOT NULL,
    confidence       REAL NOT NULL DEFAULT 0.5,
    first_seen       INTEGER NOT NULL,
    last_seen        INTEGER NOT NULL
);
CREATE UNIQUE INDEX idx_signal_unique ON scope_signal(signal_type, normalized_value);
CREATE INDEX idx_signal_scope ON scope_signal(scope_id);

CREATE TABLE scope_candidate (
    id               INTEGER PRIMARY KEY,
    user_id          TEXT NOT NULL,
    scope_type       TEXT NOT NULL,
    canonical_name   TEXT NOT NULL,
    parent_id        INTEGER REFERENCES scope(id),
    signal_type      TEXT NOT NULL,
    signal_value     TEXT NOT NULL,
    normalized_value TEXT NOT NULL,
    confidence       REAL NOT NULL DEFAULT 0.5,
    seen_count       INTEGER NOT NULL DEFAULT 1,
    status           TEXT NOT NULL DEFAULT 'pending',
    first_seen       INTEGER NOT NULL,
    last_seen        INTEGER NOT NULL,
    CHECK (status IN ('pending','promoted','rejected'))
);
CREATE UNIQUE INDEX idx_candidate_unique ON scope_candidate(
    user_id, scope_type, canonical_name, signal_type, normalized_value
);
CREATE INDEX idx_candidate_pending ON scope_candidate(user_id, status, seen_count);

CREATE TABLE fact_scope (
    fact_id  TEXT NOT NULL REFERENCES facts(fact_id) ON DELETE CASCADE,
    scope_id INTEGER NOT NULL REFERENCES scope(id) ON DELETE CASCADE,
    priority INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (fact_id, scope_id)
);
CREATE INDEX idx_fact_scope_scope ON fact_scope(scope_id);

CREATE TABLE fact_condition (
    fact_id TEXT NOT NULL REFERENCES facts(fact_id) ON DELETE CASCADE,
    key     TEXT NOT NULL,
    value   TEXT NOT NULL,
    PRIMARY KEY (fact_id, key, value)
);
CREATE INDEX idx_condition_kv ON fact_condition(key, value);

CREATE TABLE fact_origin (
    derived_fact_id TEXT NOT NULL REFERENCES facts(fact_id) ON DELETE CASCADE,
    source_fact_id  TEXT NOT NULL REFERENCES facts(fact_id) ON DELETE CASCADE,
    relation        TEXT NOT NULL,
    created_at      INTEGER NOT NULL,
    PRIMARY KEY (derived_fact_id, source_fact_id, relation)
);
CREATE INDEX idx_origin_source ON fact_origin(source_fact_id);

CREATE TABLE fact_evolution (
    from_fact_id TEXT NOT NULL REFERENCES facts(fact_id) ON DELETE CASCADE,
    to_fact_id   TEXT NOT NULL REFERENCES facts(fact_id) ON DELETE CASCADE,
    relation     TEXT NOT NULL,
    scope_id     INTEGER REFERENCES scope(id) ON DELETE SET NULL,
    reason       TEXT,
    created_at   INTEGER NOT NULL,
    PRIMARY KEY (from_fact_id, to_fact_id, relation)
);
CREATE INDEX idx_evolution_to ON fact_evolution(to_fact_id);

INSERT INTO scope(id, scope_type, canonical_name, parent_id, path, status,
                  confidence, created_at, last_seen_at)
VALUES (1, 'global', 'global', NULL, '/global', 'active', 1.0,
        CAST(strftime('%s','now') AS INTEGER) * 1000,
        CAST(strftime('%s','now') AS INTEGER) * 1000);

-- Indexes are re-created empty (see the note above the fact purge), then the
-- fact rows and their bookkeeping go. `facts` is last so the cascades on
-- fact_scope / fact_condition / fact_origin / fact_evolution and on
-- fact_reinforcements all fire against an already-clean index.
DROP TABLE IF EXISTS facts_fts;
CREATE VIRTUAL TABLE facts_fts USING fts5(
    fact_id UNINDEXED,
    text,
    tokenize = 'unicode61'
);

DROP TABLE IF EXISTS facts_vec;
CREATE VIRTUAL TABLE facts_vec USING vec0(
    fact_id TEXT PRIMARY KEY,
    embedding float[512] distance_metric=cosine
);

DELETE FROM fact_candidates;
DELETE FROM task_queue WHERE task_type IN ('extract', 'persist_pre', 'replace');
DELETE FROM facts;
