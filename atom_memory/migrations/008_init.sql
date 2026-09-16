-- dsh-atom-memory schema, migration 008 (content identity + task leases).
-- Executed by dsh_atom_memory.open_db() via PRAGMA user_version gating.
--
-- Two additions, both closing a gap between what the store *claims* to do and
-- what any code path actually did.
--
-- 1. `facts.content_fingerprint` — the content identity of a claim (see
--    `atom_memory/fingerprint.py`). Without it, every restatement of a stored
--    claim was a new row: the SPO equality check caught exact repeats for
--    single-valued predicates, but knowledge bodies and multi-valued facts had
--    no dedup at all and could be re-stored indefinitely. It is indexed by
--    (user_id, status) because every lookup is "does this owner already have an
--    active fact with this identity?".
--
-- 2. `task_queue.claimed_by` / `lease_expires_at` — a claim is now attributable
--    and time-bounded. The previous reclaim stole every `running` row
--    unconditionally, so a second consumer (a second dsh instance, a debug
--    process on the same file) would re-run work that another live worker was
--    still doing. With a lease, reclaim means "whose lease has expired", and a
--    long task can be distinguished from an abandoned one.
--
-- Neither column is exposed by a tool; both are consumed by the worker.

ALTER TABLE facts ADD COLUMN content_fingerprint TEXT;

CREATE INDEX IF NOT EXISTS idx_facts_fingerprint
    ON facts (user_id, status, content_fingerprint);

ALTER TABLE task_queue ADD COLUMN claimed_by TEXT;

ALTER TABLE task_queue ADD COLUMN lease_expires_at INTEGER;

CREATE INDEX IF NOT EXISTS idx_tasks_lease
    ON task_queue (status, lease_expires_at);
