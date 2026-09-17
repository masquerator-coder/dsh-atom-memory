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
predicates, to-dos, episodic events and knowledge are unaffected: they are
independent claims, not competing ones.

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
rejected with reason `batch_duplicate` (and recorded as a `fact_rejected` event,
so a candidate that was never written is still auditable). Inside one utterance
the store cannot know which statement supersedes which, and a visible refusal is
recoverable while a silent overwrite is not.

**Owner.** `atom_memory/conflict.py` (the pure policy), `atom_memory/validator.py`
(`_check_conflict` collects the competing rows), `atom_memory/worker.py`
(`_dedupe_batch`, `_resolve_and_write`).

**Tests.** `tests/test_conflict.py` (policy table), `tests/test_lifecycle.py`
(`test_newer_assertion_supersedes_stored_value`,
`test_weaker_contradiction_is_rejected_and_recorded`,
`test_restating_the_current_value_is_idempotent_despite_a_stale_value`,
`test_negation_flip_is_a_correction_not_a_dead_end`),
`tests/test_rpc.py::test_lifecycle_and_write_receipts_over_the_wire`.

### 2a. Which keys are single-valued at all

**Rule.** The whole policy above applies *only* under a single-valued key. A key
is multi-valued — so a different object is an independent claim, never a
correction — when any of these holds, in this order
(`validator.is_multi_valued`):

| Test | Examples | Why it is the authority |
|---|---|---|
| The **type** says so: `task`, `episodic`, or any knowledge type | a to-do, an event, a SOP | The type is the extractor's *decision about the claim*. It survives whatever predicate the extractor happens to word. |
| The **predicate** is in the built-in collection set | `偏好`/`兴趣`/`喜欢`…, `待办`/`紧急待办`/`任务`/`下一步`, `拥有项目`/`教学课程`/`日常工作线` | The measured fallback for a claim that arrived typed `semantic`. |
| The **predicate ends with a collection head noun** | `课程大纲编写事项`, `实验室采购清单` | Predicates are open vocabulary; a shape rule covers names nobody enumerated. |
| The deployment **declared** the predicate (`MemConfig.multi_valued_predicates`) | `在研课题` | The operational escape hatch: a new collision is a config line, not a release. |

Everything else is a single-valued attribute with exactly one active object.

**Why.** The rule used to be one closed list of preference predicates, and
"everything not listed is single-valued". That is a correct default *for a
controlled vocabulary* and a data-losing one for this store, whose predicates are
written by an LLM: `待办`, `任务`, `拥有项目`, `教学课程`, `日常工作线` all mean
"many of these", none was listed, and the consequence was measured on a live
store — 24 to-do candidates dropped inside their batch
(`'待办' is single-valued and already claimed in this batch`), 10 more under
`拥有项目` / `教学课程` / `日常工作线`, and one to-do overwritten
(`newer_assertion`: 低空物流 8 门新课大纲 → 教材章节索引). Nothing was visible
to the user, because a dropped candidate never became a row.

The asymmetry is what settles the direction: reading a collection as
single-valued **destroys** a memory (silently), while reading a single-valued
attribute as a collection only leaves two rows a human can merge. So the type
marker (primary), the measured predicate lists, the shape rule and the config
knob all push the same way, and the deliberate counterweight is the narrow end —
`职业`, `家乡`, `常用颜色` and friends must still be single-valued, or the store
can no longer answer with the user's *current* value, which is the one thing the
single-valued rule exists for (`test_genuine_single_valued_attributes_stay_single_valued`).

A to-do is also *not* a preference, and the two questions are answered by two
different sets: `PREFERENCE_PREDICATES` (routing in the summary view and the
profile projection) is a strict subset of `MULTI_VALUED_PREDICATES` (cardinality).
Sharing one set would render "待办: 手机真机实测" as a taste — and file the user's
outstanding work under `偏好` in the profile.

**Owner.** `atom_memory/validator.py` — `PREFERENCE_PREDICATES`,
`MULTI_VALUED_PREDICATES`, `MULTI_VALUED_PREDICATE_SUFFIXES`, `is_multi_valued`;
`atom_memory/models.py` — `TYPE_TASK`; consumed by `worker._dedupe_batch`,
`validator._check_conflict`, `api._load_conflicts` and `summary._section_of`.

**Tests.** `tests/test_validator.py`
(`test_a_second_todo_is_not_a_contradiction`,
`test_task_type_is_multi_valued_whatever_the_predicate_says`,
`test_a_deployment_can_declare_a_predicate_multi_valued`,
`test_a_collection_head_noun_is_multi_valued`,
`test_genuine_single_valued_attributes_stay_single_valued`,
`test_preference_set_is_narrower_than_the_multi_valued_set`),
`tests/test_lifecycle.py` (`test_two_todos_do_not_overwrite_each_other`,
`test_a_batch_of_todos_all_land`), `tests/test_summary.py`
(`test_a_todo_list_keeps_its_subject_and_its_items`,
`test_a_todo_is_not_rendered_as_a_preference`).

### 2b. The caller learns what was decided

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

---

## 14. Scope is an explicit dimension, and "unscoped" means global

**Rule.** A fact carries zero or more scope bindings (`fact_scope`) and zero or
more conditions (`fact_condition`); it carries no scope column of its own. A fact
with **no** binding is read as a global fact by every path — dedup, conflict,
recall and the digest.

**Why.** "Where this came from" was previously an implicit semantic property, so
the store could not tell a company-wide rule from one project's decision, could
not keep two projects' experience apart, and could not notice that three projects
had independently learned the same thing. Making it a table is what allows all
three. The unbound-is-global rule is the compatibility clause that makes the
migration survivable: rows written before the dimension existed stay meaningful
without inventing a binding, and a read path can never make a fact invisible by
failing to find one.

**Owner.** Migration 011 (`scope`, `scope_alias`, `scope_signal`,
`scope_candidate`, `fact_scope`, `fact_condition`, `fact_origin`,
`fact_evolution`), `atom_memory/context.py`, `atom_memory/scope.py`,
`docs/scopes.md`.

**Tests.** `tests/test_scope.py::test_an_unbound_fact_counts_as_global_for_both_directions`,
`tests/test_scope_writes.py::test_a_scope_blind_write_is_filed_as_global`.

---

## 15. A signal identifies one scope; weak evidence queues instead of creating

**Rule.** A scope identity is a row in `scope_signal`, unique on
`(signal_type, normalized_value)`. Resolution binds when a context's signal
matches a registered one, creates only when the level's evidence reaches
`scope_new_threshold` (the design's 高 band), and otherwise records the candidate
in `scope_candidate` and binds the fact to the nearest *known* ancestor. A queued
candidate becomes a scope after `scope_promote_after` consistent sightings, or
when the user confirms it. Creating a second scope for a signal that is already
registered is refused outright.

**Why.** Three failure modes are all the same failure: a scope tree that grows
duplicates. Two scopes for one project split its memory and neither recalls the
other; a scope created from a weak hint (a folder name) is a near-duplicate of the
one the next session would create; and a name that identifies two projects is
cross-project pollution by construction. Making the *signal* unique is what makes
resolution decidable, and queueing is what makes a wrong guess cheap: a queued
candidate costs a row, a wrong scope costs every fact filed under it.

**Owner.** `atom_memory/scope.py` (`resolve`, `_create_chain`, `_queue_candidates`,
`_promote_candidate`, `create`), `atom_memory/context.py` (the reliability table).

**Tests.** `tests/test_scope.py` — `test_a_low_confidence_signal_is_queued_not_created`,
`test_a_third_consistent_sighting_promotes_the_candidate`,
`test_a_second_different_signal_does_not_promote_the_first`,
`test_creating_a_second_scope_for_a_known_signal_is_refused`.

---

## 16. Contradiction is judged inside one scope; across scopes things are linked

**Rule.** The write path's dedup/conflict window is **exactly the write's own
scope** — not its ancestors. Inside it, the existing rules apply unchanged (a
restatement folds; a newer assertion supersedes under a single-valued key unless
the stored claim has decisively stronger evidence). Across scopes nothing is
merged and nothing is superseded: the write path records a relation instead —
`cross_scope_similar` in `fact_origin` when both hold the same object,
`exception` when one scope is an ancestor of the other, `evolves_to` otherwise.
`replace` inherits the replaced fact's scopes.

**Why.** If ancestors were in the window, a project stating its own value under a
single-valued key would supersede the global rule — and the rule every *other*
project still needs would be gone, silently, because the project's statement is
the newest one. Keeping both and ranking the specific one higher is the design's
"具体覆盖一般，保留例外": the model sees the rule and the exception instead of a
contradiction it cannot resolve. The link is also what makes promotion possible:
an independent restatement in a second project is the *evidence* that a pattern
is general, so folding it away as a duplicate destroys exactly the signal the
abstraction pass reads.

**Owner.** `atom_memory/worker.py` (`_write_scope_ids`, `_persist_fact`,
`_link_cross_scope`, `_process_replace`), `atom_memory/validator.py`
(`_check_conflict`, `cross_scope_neighbours`), `atom_memory/scope.py`
(`relate_cross_scope`).

**Tests.** `tests/test_scope_writes.py` — `test_a_project_override_does_not_supersede_the_global_rule`,
`test_a_contradiction_inside_one_scope_still_supersedes`,
`test_the_same_claim_in_two_scopes_is_two_facts_linked_by_origin`,
`test_replace_inherits_the_scope_of_the_fact_it_replaces`.

---

## 17. Recall expands upward; the digest says which level a rule belongs to

**Rule.** A scoped recall considers the scope's own path, its `phase`
descendants, facts whose conditions match the current context from anywhere else,
and global facts. A sibling scope's facts are **not** candidates. The compact
digest renders one block per level — current scope, ancestors, phases, global
rules, condition rules — each under its own budget, with the artifact capped
hard; facts bound to an invisible scope are not injected at all. The detail depth
is not blocked.

**Why.** Recall and injection answer different questions. Recall should find
anything that could be relevant, so it walks *up* the hierarchy (a project
inherits its company's rules) and down through phases (a project's own history);
injection should be short and priority-ordered, so it must also be able to leave
another project's material out entirely — and it must say *which* level each rule
lives at, because without the heading a project rule and a company rule read as
two contradicting statements. The budget is hard for the same reason it is hard
in the flat digest: a caller that asked for a token budget must never receive
more than it asked for.

**Owner.** `atom_memory/retriever.py` (`_scope_view`, `_scope_predicate`,
`_rerank`), `atom_memory/summary.py` (`_render_scoped`, `_partition_blocks`,
`_compose_blocks`).

**Tests.** `tests/test_scope.py` — `test_recall_excludes_a_sibling_project_without_a_condition_match`,
`test_recall_reaches_a_condition_matching_fact_from_another_scope`,
`test_recall_ranks_the_project_rule_above_the_global_one`,
`test_the_scoped_digest_never_exceeds_its_budget`,
`test_the_current_scope_and_the_global_rules_survive_a_squeeze`; and
`tests/test_scope.py::test_a_scope_blind_query_keeps_the_pre_scope_pipeline`,
which pins that a caller sending no context gets the pre-scope pipeline exactly.

---

## 18. A pattern several scopes arrived at becomes a global rule

**Rule.** The idle maintenance pass promotes a claim that
`scope_abstraction_min_scopes` (default 3) distinct scopes hold independently: it
writes a copy at the root (`source_type='system_inferred_high'`, inheriting the
strongest evidence among the sources), binds it to `/global`, links every
concrete fact through `fact_origin` (`relation='abstraction'`) and keeps those
concrete facts. The pass is thresholded, idempotent, and writes the promoted
row's FTS and vector entries.

**Why.** A pattern three different projects arrived at is not a project detail, it
is how the work is done — and leaving it inside those three projects means the
fourth re-learns it. It runs from maintenance rather than the write path because
it is a store-wide judgement no single write can make, and because the promoted
row needs an embedding like any other fact: a rule that search cannot find is not
a rule. The concrete facts stay because they are the evidence for the rule and
the place a project-specific nuance remains visible.

**Owner.** `atom_memory/worker.py` (`promote_abstractions`,
`_write_abstracted_fact`, `maintenance`), `atom_memory/scope.py`
(`abstraction_candidates`).

**Tests.** `tests/test_scope_writes.py` — `test_the_abstraction_pass_promotes_a_shared_claim_and_indexes_it`
(including its idempotence and a zero-orphan index check),
`test_promotion_stays_off_with_scope_awareness_disabled`;
`tests/test_scope.py::test_abstraction_requires_independent_scopes`.
