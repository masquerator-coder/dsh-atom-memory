-- dsh-atom-memory schema, migration 013 (the memory-changelog surface and the
-- cached work overview).
-- Executed by dsh_atom_memory.db.open_db() via PRAGMA user_version gating.
--
-- Two additions, one reason: the injected memory summary is becoming a *work
-- overview* (what has been worked on, and how to look up the detail) instead of
-- a list of atomic facts. That overview is written by a model call, so it must
-- be generated out of band and cached — and "is it worth generating again?"
-- needs a cheap, queryable record of what actually changed in the store.
--
-- ---------------------------------------------------------------------------
-- The changelog rides on the existing `events` table.
-- ---------------------------------------------------------------------------
-- `events` (001_init.sql) is already the audit log: `db.record_event` writes it
-- from every policy path (fact_superseded, fact_rejected, fact_deduplicated,
-- scope_merged, facts_archived, ...). Reusing it keeps one append-only history
-- rather than two, and needs no table of its own.
--
-- It did have one hole: a *successfully written* fact wrote nothing at all, so
-- the log recorded refusals and reorganisation but not "the store grew". That is
-- exactly the event the overview refresh keys off, so the write path now emits
-- `fact_written`. Reuse needs no new event of its own: a restated fact is folded
-- into the row it matches and already recorded as `fact_deduplicated`, which the
-- classifier treats the same way (`detail`, not `structural`). This migration
-- contributes the index that makes reading "what changed since T" cheap; the
-- rows come from the code.
--
-- The existing idx_events_user_type is (user_id, type), which cannot serve a
-- time-ordered scan of one user's recent changes — a range on created_at would
-- still have to sort. This index puts created_at in the key, so the changelog
-- read is an index range scan and no sort.
--
-- `CREATE INDEX IF NOT EXISTS` alone is not enough: it tolerates the index
-- already existing but still fails outright when the *table* is missing, and a
-- legacy database can legitimately reach here without `events`. Migration 001
-- creates it, but a database whose history was rebuilt by a tool that scripted
-- only the tables a later migration alters arrives with `user_version` at an old
-- number and no `events` table at all. Aborting there would leave the store
-- unopenable, so the table is created when absent. The definition is migration
-- 001's verbatim — a repair for an incomplete history, not a redefinition — so a
-- database that already has the table is untouched.
CREATE TABLE IF NOT EXISTS events (
    event_id   TEXT PRIMARY KEY,
    user_id    TEXT NOT NULL,
    type       TEXT NOT NULL,
    payload    TEXT NOT NULL,
    trace_id   TEXT,
    created_at INTEGER NOT NULL
);
-- Unconditional: 001 created this one alongside the table, and a history that
-- could lose the table could lose the index with it.
CREATE INDEX IF NOT EXISTS idx_events_user_type ON events(user_id, type);
CREATE INDEX IF NOT EXISTS idx_events_user_created ON events(user_id, created_at DESC);

-- ---------------------------------------------------------------------------
-- `memory_overview` — one cached work overview per user.
-- ---------------------------------------------------------------------------
-- One row per user (user_id is the primary key), because the overview answers
-- "what has this user been working on", which is a property of the whole store
-- rather than of one scope: a per-scope cache would multiply model calls by the
-- number of projects and still leave "and what else have I done" unanswerable.
--
-- `fingerprint` is the authoritative staleness check. It is a digest of the
-- inputs the text was generated from (active fact count, newest write and reuse
-- timestamps, the scope set, the topic set), so a cached row is used only while
-- it still describes the store — "the overview changed" and "the store changed"
-- are different questions, and this column is what keeps a stale render from
-- being served as current.
--
-- `facts_count` is kept alongside the fingerprint because it is the one
-- staleness input a human can read at a glance, and `source` records whether the
-- text came from the model or from the deterministic fallback render — a
-- deployment that never runs the model path can still tell the two apart.
--
-- ---------------------------------------------------------------------------
-- This migration does NOT clear anything, and does NOT back-fill.
-- ---------------------------------------------------------------------------
-- 011_init.sql:51 recorded that its fact purge was a one-time consequence of
-- introducing the scope dimension and that future migrations must never repeat
-- it. This one honours that: an existing deployment gets an empty overview cache
-- (the first freeze falls back to the deterministic render until the out-of-band
-- job fills it) and a changelog that starts from now, which is all either
-- consumer needs.

CREATE TABLE IF NOT EXISTS memory_overview (
    user_id     TEXT PRIMARY KEY,
    text        TEXT NOT NULL,
    fingerprint TEXT NOT NULL,
    facts_count INTEGER NOT NULL DEFAULT 0,
    source      TEXT NOT NULL DEFAULT 'llm',
    created_at  INTEGER NOT NULL,
    updated_at  INTEGER NOT NULL
);