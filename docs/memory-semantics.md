# Memory semantics

The behavioural contract of the store: what happens to a memory when it is
written, contradicted, reused, outgrown, or read back into a prompt. The
[Python library contract](python-library.md) describes calls and shapes and
[`dsh/README.md`](../dsh/README.md) describes the dsh-side surface; this document
describes the *policies* those surfaces implement, because a memory system is
defined by what it decides, not by what it stores.

Every policy below is stated as a rule, followed by the reason it is the rule,
the module that owns it, and the test that holds it in place. Where a policy
replaced a previous behaviour, the old behaviour is named — those are the
choices that were wrong, and knowing them is what keeps them from coming back.

---

## 1. A write is atomic, and a failed write leaves no trace

**Rule.** One fact is one transaction: the `facts` row, its `fact_fts` row and
its `facts_vec` row are committed together, or not at all. Any failure rolls the
connection back before the task is recorded as failed, and the failure is
written onto the *candidate* (`status='error'`, `reject_kind`, `reject_reason`)
so the caller can see why nothing was stored.

**Why.** Hybrid retrieval means one fact lives in three places. A partial write
is not "less memory" — it is memory that vector search cannot see, or a ghost
that full-text search returns while `facts` no longer has it. Every downstream
consumer (`recall`, `summary`, the profile projection, the settings panel) trusts
the three indexes to agree, so the invariant has to hold at the only place that
can guarantee it.

**Owner.** `atom_memory/worker.py` — `_persist_fact` (single transaction),
`_handle_task` (rollback on failure), `_finish_candidate` (outcome recording).

**Tests.** `tests/test_lifecycle.py::test_failed_vector_write_leaves_no_partial_fact`
(injects a wrong-dimension embedder so the third insert fails, then asserts zero
rows in `facts`/`fact_fts`/`facts_vec` and a recorded `task_error`),
`tests/test_integration.py::test_persist_pre_rolls_back_on_vector_error`.

### 1a. The store also *repairs* itself

**Rule.** At startup, and periodically while idle, the worker looks for facts
whose index rows are missing and re-creates them, and drops index rows whose fact
is gone. The pass is idempotent and reports what it fixed.

**Why.** Prevention covers the writer; it does not cover backups restored by
hand, a killed process, or a database written by an older version. Without a
repair pass those rows are permanently invisible to one half of the retrieval
pipeline, and nothing in the system can tell the difference between "this memory
does not exist" and "this memory is unreachable".

**Owner.** `atom_memory/worker.py` — `maintenance`, `repair_index`.
**Tests.** `tests/test_lifecycle.py::test_maintenance_repairs_a_fact_whose_index_entries_are_missing`.

---

## 2. A newer assertion replaces the stored value; weaker evidence does not

**Rule.** Under a **single-valued** predicate, a contradicting claim is resolved
by evidence, not by arrival order alone:

| Situation | Outcome |
|---|---|
| Candidate matches a stored claim exactly (same object, same negation) | Idempotent — the stored fact is reinforced, nothing is written |
| Candidate's evidence weight ≥ stored − `conflict_confidence_margin` (0.05) | **Supersede**: the new fact is written, every stored claim under that key is retired with `superseded_by`, and a `fact_superseded` event records both values |
| Candidate's evidence weight < stored − margin | **Reject**, with the refusal recorded as a `fact_rejected` event and reported in the write receipt |
| Multiple stored claims under one key (legacy or imported data) | Compared against the **strongest** one; a supersede retires all of them |

Evidence weight is `0.7 × confidence + 0.3 × importance`. Multi-valued
predicates, episodic events and knowledge are unaffected: they are independent
claims, not competing ones.

**Why.** The previous behaviour refused every contradiction and kept the older
row. That is wrong twice over: the user's newest statement — "I moved to Beijing
last month" — was silently discarded, and the store kept asserting the value the
user had just corrected. But the opposite extreme (last write wins) is also
wrong: a low-confidence extraction from a passing remark would overwrite a fact
the user stated explicitly. The margin makes the intent legible — *a correction
lands when the evidence is comparable, a weak claim never displaces a stronger
one* — and both outcomes are recorded, so the refusal is auditable rather than
invisible.

Within a single extraction batch, two candidates for the same single-valued key
are collapsed first: the higher-evidence one is kept, the other is reported as
rejected with reason `batch_duplicate`. Inside one utterance the store cannot know
which statement supersedes which, and a visible refusal is recoverable while a
silent overwrite is not.

**Owner.** `atom_memory/conflict.py` (the pure policy), `atom_memory/validator.py`
(`_check_conflict` collects the competing rows), `atom_memory/worker.py`
(`_dedupe_batch`, `_resolve_and_write`).

**Tests.** `tests/test_conflict.py` (policy table), `tests/test_lifecycle.py`
(`test_newer_assertion_supersedes_stored_value`,
`test_weaker_contradiction_is_rejected_and_recorded`,
`test_restating_the_current_value_is_idempotent_despite_a_stale_value`,
`test_negation_flip_is_a_correction_not_a_dead_end`),
`tests/test_rpc.py::test_lifecycle_and_write_receipts_over_the_wire`.

### 2a. The caller learns what was decided

**Rule.** `memory_add`, `memory_replace` and `memory_forget` accept `wait_ms` and
return the store's verdict once the candidate reaches a terminal state (or the
enqueue receipt when the wait expires). The verdict names what was written, what
was replaced (old → new), and what was refused and why.

**Why.** The write pipeline is asynchronous, but the *decision* inside it is what
the caller has to know: "queued" and "refused because a better-evidenced claim is
stored" are different facts about the world. The bounded wait keeps the pipeline
asynchronous (it is an acknowledgement, not a synchronous write) while removing
the failure mode where the model believes it saved something the store rejected.

**Owner.** `atom_memory/api.py` — `_write_receipt`, `recent_outcomes`;
`atom_memory/worker.py` — `_finish_candidate` writes
`result_fact_ids`/`reject_kind`/`reject_reason`/`finished_at`.

**Tests.** `tests/test_lifecycle.py::test_write_receipt_reports_the_outcome`,
`dsh/tests/tools-outcome.test.ts`.

---

## 3. Nothing is destroyed by policy; deletion is explicit

**Rule.** Memory has three tiers and two removal modes:

| Tier | Meaning | Reaches the model |
|---|---|---|
| `active` | Current belief | Always |
| `superseded`, `retracted` | Was believed, or was withdrawn by the user | Never; listed by `list_facts(include_retracted=True)` |
| `archived` | Displaced by capacity control, worth keeping | Never, until `unarchive` |

Removal: **retract** (`forget`, default) is soft and reversible; **purge**
(`forget(purge=True)`, or `forget_all(purge=True)`) deletes the rows, their index
entries and their reinforcement log irreversibly.

**Why.** A memory system that deletes on its own is one nobody can trust with
anything important, and a memory system that never deletes cannot honour a
deletion request or bound its own file. Splitting the two makes each honest: the
automatic tier (`archived`) is lossless and reversible, and the destructive one
requires someone to ask for it by name.

**Owner.** `atom_memory/api.py` — `forget`, `unarchive`, `purge`;
`atom_memory/worker.py` — `purge_facts`.

**Tests.** `tests/test_lifecycle.py::test_forget_purge_removes_rows_and_index_entries`,
`test_soft_forget_keeps_the_row_recoverable`, `test_archive_and_unarchive`.

---

## 4. Capacity control and retention are bounded, and they protect what matters

**Rule.** When `max_active_facts` is reached, the maintenance pass moves the
least valuable **unprotected** facts to `archived` until the store fits. The
value order is the memory's own priority order (effective importance with
reuse-and-decay applied); a fact is *protected* when any of these hold:

1. it is newer than `archive_protect_days` (default 30) — anything recently
   stated is part of the current context;
2. it has been reused (`reinforce_count > 0` / a `fact_reinforcements` row) —
   reuse is the strongest available signal of value;
3. its type is durable knowledge (`decision_rule`, `lesson`, `sop`) — the
   expensive things to re-derive;
4. it backs a pinned profile row — the user asked for that row to stand.

Terminal rows are pruned by age (`candidate_retention_days` 7,
`task_retention_days` 14, `event_retention_days` 180); reinforcement evidence is
**not** pruned with them, because it is the replay log that justifies a decay
score.

**Why.** A cap with no policy is a silent data-loss mechanism (the classic
"oldest row wins" eviction is exactly wrong: the oldest fact in a long-lived
store is often the most load-bearing), and a cap with no protection class is what
turns a personal memory into a FIFO buffer. Archiving rather than deleting keeps
the failure mode recoverable. The retention defaults are deliberately asymmetric:
discarding a candidate row costs an audit trail, discarding reinforcement
evidence costs the ability to explain a ranking.

**Owner.** `atom_memory/worker.py` — `enforce_capacity`, `_prune_table`,
`maintenance`; configuration in `atom_memory/config.py`.

**Tests.** `tests/test_lifecycle.py::test_capacity_archives_the_least_valuable_fact`,
`test_capacity_protects_fresh_reinforced_durable_and_pinned_facts`,
`test_maintenance_prunes_terminal_rows`.

---

## 5. Retrieved memory is data, and is rendered as data

**Rule.** Two layers, both mandatory:

- **Ingest** (`atom_memory/sanitize.py`): before a candidate is validated or
  stored, invisible characters are removed (bidi controls, zero-width joiners,
  BOM/soft hyphen, tag characters, other control characters), tabs become spaces,
  newlines are normalised, `subject`/`predicate`/`object` are forced to a single
  line, and every field is length-capped. The same cleaning applies on the
  `edit_fact` and `upsert_profile` paths and on backup import.
- **Egress** (`dsh/src/memory-data.ts`): before injection, the digest is cleaned
  again, wrapped in a fence token that cannot occur inside it, and **every line is
  prefixed** so that no stored line can occupy column zero — where `#`, `system:`
  and `<|…|>` acquire meaning. The header states the contract with the data.

ZWJ/ZWNJ are deliberately preserved: they join adjacent glyphs (emoji sequences,
Indic and Persian orthography) and cannot carry a readable instruction, so
stripping them would mangle real content for no security gain. Content is never
NFKC-normalised — normalising would rewrite what the user stored.

**Why.** Memory content originates in user input, and the system prompt is the
highest-trust channel there is; "please treat the following as data" is a request,
not a mechanism. Length caps and invisible-character removal also protect the
*store*: an unbounded field is a denial-of-service on embedding, the summary
budget and the settings panel. Two layers because they fail differently — the
ingest layer stops a disguised instruction from ever being stored where another
consumer (the panel, a backup, another client) might render it unprotected, and
the egress layer makes even a store that already contains one render inert.

**Owner.** `atom_memory/sanitize.py`, `atom_memory/validator.py` (`validate`
normalises before it checks), `dsh/src/memory-data.ts`, `dsh/src/context.ts`.

**Tests.** `tests/test_sanitize.py`, `dsh/tests/memory-data.test.ts`.

---

## 6. Relevance is absolute, and "nothing is relevant" is an answer

**Rule.** Fusion stays RRF (`rrf_merge`), but the fused score is normalised
against its *ceiling* — the score two top-ranked hits would produce,
`2/(k+1)` — not against the best candidate in the current result set:

```
relevance = min(1, rrf / (2 / (k + 1)))
```

Two filters can then mean something, and both are configurable:

- `max_vector_distance` (default 0.70) drops candidates whose *cosine distance*
  is too large — the one signal that can say "this is a different topic area";
- `min_relevance` (default 0, off) drops candidates below a fused-relevance
  floor.

The remaining terms stay absolute: `importance` and `recency` are never
min-max normalised (their magnitude is the point), and the reuse term is bounded.

**Why.** `_minmax` over the current candidates made the top result of *every*
query score exactly 1.0 — including the best of twenty irrelevant rows — so the
score could not be compared across queries, could not be thresholded, and made a
single-candidate result look like a perfect match. Replacing it with rank-only
filtering does not work either: every candidate that reached either top-k has a
rank-based score of at least ~0.43, so a rank floor filters nothing. The distance
gate is measured, not guessed (related pairs 0.33–0.54, cross-language related
~0.65, unrelated 0.67–0.85 on `BAAI/bge-small-zh-v1.5`), and the store tells the
caller when an index was unavailable (`degraded`) so a partial answer is not
mistaken for an empty one.

**Owner.** `atom_memory/retriever.py` — `relevance_from_rrf`, `rrf_ceiling`,
`_rerank`, `search`, `last_degraded`.

**Tests.** `tests/test_retriever.py::test_relevance_is_absolute_not_min_max`,
`test_a_single_candidate_no_longer_scores_full_relevance`,
`test_vector_distance_gate_filters_distant_rows`,
`test_min_relevance_makes_nothing_relevant_a_valid_answer`,
`test_degraded_is_reported_when_an_index_is_unavailable`.

---

## 7. The profile is a table the user owns

**Rule.** `user_profile` is an independent, persistent table. Rows enter it only
when the user accepts a generated suggestion or types one, and leave it only
when the user deletes one. Facts are never a writer: reading the profile does
not rebuild it, and learning a fact does not add, update or remove a row.
Entries whose source fact is later retracted stay until the user removes them.

Generation is user-triggered and never writes: the aggregation in
`profile_candidates` offers the slots the active facts imply, the LLM (on the
dsh side) curates them into proposals, and `write_profile` is what actually
stores the ones the user approved. The table is capped
(`MemConfig.max_profile_rows`, default 50) and the render is capped separately
(`user_md_token_limit`); a saturated table refuses *new* keys but keeps
accepting edits to existing rows, and the render says how many rows it could not
fit.

**Why.** The profile used to be a projection rebuilt on every read, which made
the panel's controls partly fictional in two directions at once: deleting a row
came back on the next read (the source fact was still active, so it was
re-derived), and a row whose fact had been retracted was never removed (the
projection only ever upserted). Both follow from the table being a *cache* — the
user's edits were addressed to the cache, not to the memory. Making it a table
the user owns is what makes an edit mean what the user expects. It also bounds
the cost honestly: the profile is rendered into the session system prompt, so
every row is paid for on every request, and a cap plus a render budget is what
keeps that from growing on its own.

**Owner.** `atom_memory/profile.py` — `write_profile_rows`,
`delete_profile_row`, `suggestible_profile_entries`, `profile_md`;
`atom_memory/api.py` — `list_profile`, `profile_candidates`, `write_profile`,
`upsert_profile`, `delete_profile`, `user_md`; `dsh/src/profile-synthesis.ts` —
the curation prompt.

**Tests.** `tests/test_ui_api.py::test_learning_a_fact_does_not_create_a_profile_row`,
`test_profile_rows_are_capped`, `test_write_profile_batch_is_all_or_nothing`;
`tests/test_lifecycle.py::test_learning_facts_does_not_touch_the_profile`;
`tests/test_retriever.py::test_suggestible_entries_mirror_the_old_projection_rule`.

---

## 8. A switch is read when it is used

**Rule.** The live settings (`enabled`, `captureEnabled`, `llmExtractionEnabled`,
`contextInjectionEnabled`, `injectedSummaryTokens`, `extractionModel`) reach
their consumers as *getters* and are resolved at the moment of use — per message,
per prompt assembly, per freeze, per extraction call.

**Why.** A switch captured at registration time applies at the next restart,
which in practice means "never" for a session already in progress: the settings
panel would appear to work while the running session kept its old behaviour. Two
consequences are easy to miss and are the reason this is stated as a policy rather
than an implementation detail: injection can be turned back **on** mid-session
(the listener is always registered and the gate is inside it), and an
already-frozen session keeps its frozen text — the switch governs the *next*
freeze, because re-rendering mid-session would invalidate the prompt prefix and
the provider's KV cache.

**Owner.** `dsh/src/runtime.ts`, `dsh/src/context.ts`, `dsh/src/capture.ts`,
`dsh/src/index.ts`.

**Tests.** `dsh/tests/context.test.ts::honours the injection switch live, in both
directions`, `dsh/tests/capture.test.ts::registers the hooks and honours the
switch live, in both directions`.

---

---

## 9. The plugin knows whether its backend exists, and says so

**Rule.** Before the bridge is trusted, the configured interpreter is probed once
with `-c "import atom_memory, sqlite_vec"`. A **permanent** failure (missing
module, missing or unexecutable interpreter, permission error) is reported once,
with the failing module and the remedy, and is **not** retried; a **transient**
failure (slow or timed-out probe) keeps the normal three-attempt budget. Health is
reported as a payload (`ok`, queue depths, index consistency, database path,
last error), not a boolean, and the panel shows the reason the bridge is down.

**Why.** "Starts fine, then fails every call" is the worst failure mode a plugin
can have: the tools look registered, the model looks capable, and each call fails
for a reason nobody can see. A boolean health check has the same problem in
smaller form — "unhealthy" without the reason is not actionable. Distinguishing
permanent from transient is what stops a typo in `pythonBin` from being retried
into a log full of identical errors.

**Owner.** `dsh/src/preflight.ts`, `dsh/src/bridge.ts` (`healthDetail`),
`dsh/src/controller.ts` (`health`), `atom_memory/rpc.py` (`_health`).

**Tests.** `dsh/tests/preflight.test.ts`,
`tests/test_rpc.py::test_lifecycle_and_write_receipts_over_the_wire`.

---

## 10. A memory keeps its identity, so restating it is reuse, not a second row

**Rule.** Every fact is stamped with a **content fingerprint**: the normalised
identity of its claim — owner, type, subject, predicate, object, and claim
polarity. For knowledge types (`lesson` / `sop` / `few_shot`) the identity is the
**body**, because their `object` is a derived label (the compact summary titles
them from the body's first line), so two captures of one procedure routinely
carry different titles for identical text.

A write whose fingerprint matches an active fact **reinforces** that fact and
writes a `fact_deduplicated` event; it does not add a row. A second, narrower
gate covers a *reworded* body: for bodies long enough to be a document
(`dedup_min_body_chars`), the candidate's own embedding is compared against
active facts sharing owner, type, subject and predicate, and a distance inside
`dedup_max_distance` (0.10) also folds the write.

**Why.** Before this, only an exact SPO repeat of a *single-valued* predicate was
recognised. Knowledge and multi-valued facts could be stored again and again —
the single largest source of noise in the store, and the reason "the same thing
said slightly differently" occupied several recall slots. The gates are
deliberately asymmetric in strictness because the failure modes are: a false merge
removes a distinct memory from the working set (recoverable only from the
reinforcement log), while a missed merge costs one redundant row.

**Owner.** `atom_memory/fingerprint.py`, `atom_memory/worker.py`
(`_persist_fact` → `_fold_into`, `_near_duplicate`), migration 008.

**Tests.** `tests/test_hardening.py` — same body/two titles folds on the
fingerprint; a reworded body folds on the embedding; a different document does
not; `dedup_max_distance = 0` disables only the semantic half; polarity and owner
are part of the identity; the fingerprint is persisted on the row.

---

## 11. What the store changes about your text, it reports

**Rule.** Normalisation that *loses* information — the field and body caps — is
reported rather than applied silently. `clean_body_meta` / `clean_field_meta`
return the text plus its original length and a truncation flag; the validator
attaches the records to every result; `add`, `replace`, `edit_fact`,
`upsert_profile` and the worker's write outcome all carry them, and the receipt
renders "kept the first 20 000 characters of 40 000".

**Why.** Whitespace and invisibility normalisation is reversible in effect; a cap
is not. A store that quietly keeps a prefix of what the user said will later
answer questions about text it does not have, and the caller has no way to notice
— the same class of failure as reporting "queued" for a write that was refused.

**Owner.** `atom_memory/sanitize.py` (`Cleaned`), `atom_memory/validator.py`,
`atom_memory/api.py`, `dsh/src/tools.ts` (`renderWriteReceipt`).

**Tests.** `tests/test_hardening.py` — a capped write reports field/original/kept;
an untouched write reports an empty list (the outcome shape is stable); the panel
write paths report too.

---

## 12. Work is claimed once, and reclaimed only when it is abandoned

**Rule.** A task claim is **attributable** (`claimed_by`) and **time-bounded**
(`lease_expires_at`, `task_lease_sec`). Claiming is a compare-and-swap on
`status = 'pending'` whose row count decides the winner. Reclaim on start touches
only rows whose lease has expired (for pre-lease rows, only those older than the
starting worker). Finishing or failing a task clears its claim.

**Why.** The previous reclaim reset *every* `running` row, so a second consumer
over one database — a second dsh instance, a debugging process — re-ran work
another live worker was in the middle of. That is silent double execution of a
write path. Delivery remains at-least-once: a task that legitimately outlives its
lease may be re-run, which is the trade-off a lease buys.

**Owner.** `atom_memory/worker.py` (`_claim_next_task`, `start`, `_record_failure`,
`_requeue_inflight`), migration 008.

**Tests.** `tests/test_hardening.py` — a live peer's claim is left alone while an
expired lease is reclaimed; a finished task holds no claim.

---

## 13. The recall budget is bounded, and the rest stays reachable

**Rule.** `max_fact_tokens` caps one fact inside a recall result. A longer body is
shortened to fit, the fact is returned with `truncated: true`, and its full text
stays available through `get_fact` (the `memory_get` tool). The first match is
still always kept, so a tiny budget still returns something.

**Why.** The budget was soft: keeping the first fact unconditionally let a single
long SOP overshoot a 200-token budget by ~60x in measurement, and captured
knowledge is exactly where long bodies come from. Truncating without a fetch path
would just be data loss with extra steps, which is why the cap and `get_fact`
ship together.

**Owner.** `atom_memory/api.py` (`recall`, `get_fact`),
`atom_memory/retriever.py` (`truncate_to_tokens`), `dsh/src/tools.ts`
(`memory_get`, recall render).

**Tests.** `tests/test_hardening.py` — an oversized body comes back shortened and
flagged while `get_fact` returns it whole; a body under the ceiling is untouched;
`get_fact` refuses another user's fact.
