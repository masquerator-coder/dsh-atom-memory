-- dsh-atom-memory schema, migration 007 (write-path outcomes, archive tier).
-- Executed by dsh_atom_memory.open_db() via PRAGMA user_version gating.
--
-- Three things this migration makes possible, all of them about the memory
-- *lifecycle* rather than any single feature:
--
-- 1. `fact_candidates` gains the write outcome. Until now a candidate's status
--    was the only signal, and it was written `applied` even when every
--    extracted candidate had been rejected, so a rejected write looked exactly
--    like a successful one. `reject_kind` / `reject_reason` say why nothing was
--    written, `result_fact_ids` names what was, and `finished_at` is when the
--    worker finished the unit of work (which is what a caller waits on when it
--    asks for a synchronous outcome instead of the enqueue receipt).
--
-- 2. `facts.archived_at` backs the archive tier. Facts are never physically
--    deleted, so a capacity policy cannot free space by deletion; it moves the
--    *least valuable, least protected* active facts to `status = 'archived'`,
--    which every existing read path already excludes (they all filter
--    `status = 'active'`). `archived_at` makes the tier auditable and lets a
--    user restore a fact with `unarchive`.
--
-- 3. Two indexes the new maintenance passes need: terminal candidates are swept
--    by (status, created_at), and the archive pass orders by the archive key.

ALTER TABLE fact_candidates ADD COLUMN reject_kind TEXT;
ALTER TABLE fact_candidates ADD COLUMN reject_reason TEXT;
ALTER TABLE fact_candidates ADD COLUMN result_fact_ids TEXT;
ALTER TABLE fact_candidates ADD COLUMN finished_at INTEGER;

ALTER TABLE facts ADD COLUMN archived_at INTEGER;

CREATE INDEX idx_cand_status_created ON fact_candidates(status, created_at);
CREATE INDEX idx_facts_user_archived ON facts(user_id, status, archived_at);
