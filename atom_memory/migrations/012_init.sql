-- dsh-atom-memory schema, migration 012 (the topic dimension).
-- Executed by dsh_atom_memory.db.open_db() via PRAGMA user_version gating.
--
-- Scope answers "in which context" (project, course, chapter, document, client)
-- and is inferred from the environment. It cannot answer "about what topic",
-- and the failure that costs is the same shape the scope dimension was built to
-- remove, one axis over:
--
--   * a durable preference of the user ("likes coffee") is bound to whatever
--     project it was said in, so it is invisible in every other project;
--   * a life/travel fact bound to a project-scope the user happens to be in is
--     *visible* in that project and competes with the work that belongs there;
--   * nothing records that a claim is about teaching rather than about
--     programming, so the two cannot be separated inside one context.
--
-- Domains are the second dimension: multi-label, hierarchical, per user, and
-- **registered** rather than free-form, because a tag vocabulary that grows
-- without a gate stops being a vocabulary.
--
-- The four tables:
--
--   domain          one node per (user, canonical name); `parent_id` is the
--                   tree, `path` is a label (same decision as `scope`: canonical
--                   names may contain separators, so parsing a path back into a
--                   tree is ambiguous the first time one does).
--   fact_domain     facts <-> domains (many-to-many), with the *primary* label
--                   that ranking and conflict judgement read, and `source`,
--                   which records which rule chose the label (extractor hint,
--                   scope mapping, keyword, session default). The source is what
--                   makes "semi-automatic tagging" auditable and correctable.
--   domain_signal   the registration queue: a domain the extractor proposed but
--                   that no ancestor covers. Counted, then promoted by a human
--                   or an admin — never created silently.
--   domain_bridge   cross-topic links. They only ever contribute *ranking*
--                   weight; they never widen a filter set. Giving them a filter
--                   role is how "topic isolation" and "reachable across topics"
--                   start contradicting each other.
--
-- ---------------------------------------------------------------------------
-- This migration does NOT clear anything, and does NOT back-fill.
-- ---------------------------------------------------------------------------
-- Migration 011 cleared the fact store and left an explicit note that future
-- migrations must never repeat it (011_init.sql:51). This one honours that:
-- existing facts simply have no `fact_domain` row, which every domain-aware read
-- path must treat as "unlabelled" and keep visible (the compatibility rule that
-- `fact_scope`'s unbound case establishes). A deployment switching the domain
-- filter on therefore degrades to today's behaviour rather than losing facts,
-- and the label vocabulary is built from the scope tree that already exists
-- (see atom_memory.domain.seed_from_scopes) instead of from a static list.

CREATE TABLE domain (
    id             INTEGER PRIMARY KEY,
    user_id        TEXT NOT NULL,
    canonical_name TEXT NOT NULL,
    display_name   TEXT NOT NULL,
    parent_id      INTEGER REFERENCES domain(id),
    path           TEXT NOT NULL,
    status         TEXT NOT NULL DEFAULT 'active',
    merged_into    INTEGER REFERENCES domain(id),
    system_seeded  INTEGER NOT NULL DEFAULT 0,
    created_at     INTEGER NOT NULL,
    last_seen_at   INTEGER NOT NULL,
    CHECK (status IN ('active','merged','archived'))
);
CREATE INDEX idx_domain_parent ON domain(user_id, parent_id);
CREATE INDEX idx_domain_path   ON domain(user_id, path);
-- One node per name per user: two resolutions of the same name must land on the
-- same row instead of racing two rows into existence (the rule `scope` follows).
CREATE UNIQUE INDEX idx_domain_unique ON domain(user_id, canonical_name);

CREATE TABLE fact_domain (
    fact_id    TEXT NOT NULL REFERENCES facts(fact_id) ON DELETE CASCADE,
    domain_id  INTEGER NOT NULL REFERENCES domain(id) ON DELETE CASCADE,
    confidence REAL NOT NULL DEFAULT 0.5,
    is_primary INTEGER NOT NULL DEFAULT 0,
    -- Which rule chose this label: user_explicit / hint / scoped_map / keyword /
    -- session_default / seed. Recorded per row, not per fact: one fact may be
    -- labelled by the extractor and corrected by the user in the same breath.
    source     TEXT NOT NULL DEFAULT 'session_default',
    created_at INTEGER NOT NULL,
    PRIMARY KEY (fact_id, domain_id)
);
CREATE INDEX idx_fact_domain_domain ON fact_domain(domain_id, fact_id);
CREATE INDEX idx_fact_domain_fact   ON fact_domain(fact_id);

CREATE TABLE domain_signal (
    id               INTEGER PRIMARY KEY,
    user_id          TEXT NOT NULL,
    canonical_name   TEXT NOT NULL,
    nearest_ancestor INTEGER REFERENCES domain(id),
    scope_id         INTEGER REFERENCES scope(id),
    -- A scope-less sighting stores NULL, and SQLite treats NULLs as *distinct*
    -- in a UNIQUE index — so the unique key uses this generated column instead
    -- of `scope_id`. Without it the second sighting would insert a second row
    -- rather than count the first, and that count is the entire basis for
    -- offering a name for registration.
    scope_key        INTEGER GENERATED ALWAYS AS (COALESCE(scope_id, -1)) VIRTUAL,
    seen_count       INTEGER NOT NULL DEFAULT 1,
    status           TEXT NOT NULL DEFAULT 'pending',
    first_seen       INTEGER NOT NULL,
    last_seen        INTEGER NOT NULL,
    CHECK (status IN ('pending','promoted','rejected'))
);
-- Consistent sightings bump one row; a different scope under the same name is a
-- different row and cannot promote the first (the `scope_candidate` rule).
CREATE UNIQUE INDEX idx_domain_signal_unique
    ON domain_signal(user_id, canonical_name, scope_key);
CREATE INDEX idx_domain_signal_pending
    ON domain_signal(user_id, status, seen_count);

CREATE TABLE domain_bridge (
    user_id TEXT NOT NULL,
    from_id INTEGER NOT NULL REFERENCES domain(id) ON DELETE CASCADE,
    to_id   INTEGER NOT NULL REFERENCES domain(id) ON DELETE CASCADE,
    weight  REAL NOT NULL DEFAULT 0.5,
    PRIMARY KEY (user_id, from_id, to_id)
);
CREATE INDEX idx_domain_bridge_from ON domain_bridge(user_id, from_id);
