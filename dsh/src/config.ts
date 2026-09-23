/**
 * Plugin configuration (schemastery). See the repo design doc for the
 * rationale of each field. All fields are optional with safe defaults so the
 * plugin behaves sanely when only `dbPath` is provided.
 *
 * @module dsh-atom-memory/config
 */
import z from '@deepseek-ai/schemastery'
import { DEFAULT_INJECTED_SUMMARY_TOKENS } from './injection-budget.ts'

/**
 * A settings-owned reference whose `get()` returns the current snapshot.
 *
 * Declared structurally rather than imported: the canonical definition lives in
 * `@deepseek-ai/cosmokit` (`lib/types/volatile.d.ts`), which is not a dependency
 * of this plugin, and the harness's own plugins reach it through cordis's
 * re-export — which this cordis version (4.0.2) does not provide. The shape is
 * a single method, so restating it here costs nothing and keeps the plugin free
 * of a dependency it would otherwise need only for a one-line interface.
 */
export interface Volatile<T> {
  /** @returns the current immutable snapshot. */
  get(): T
}

/**
 * The live half of the plugin configuration, as the runtime sees it.
 *
 * Every field here is `.volatile()` in {@link Config}, which changes its TYPE:
 * schemastery's output for a volatile node is `Volatile<T>` — a stable
 * reference whose `get()` returns the current snapshot — not a plain value. The
 * harness's own plugins declare the same shape (see
 * `packages/client/ui-theme/src/index.ts`), and reading through `get()` is what
 * makes a settings-panel edit visible to the plugin without a reload.
 *
 * This matters for the panel to exist at all: DSH's settings layer builds a
 * plugin's page from `volatileForm(schema)`, which only descends into nodes
 * marked volatile. A Config with no volatile field yields no form, and
 * `describe()` then omits the namespace from the served list entirely — the
 * browser panel finds nothing, reports itself unavailable, and every control it
 * guards renders disabled.
 */
export interface LiveConfig {
  /** Manual LLM extraction model override; empty provider+model = follow dsh default. */
  extractionModel: Volatile<ExtractionModelConfig>
  /** Whether the session/durable capture hooks (turn/end, user/message) run. */
  captureEnabled: Volatile<boolean | undefined>
  /** Whether the LLM-first extractor is wired to the dsh default model. */
  llmExtractionEnabled: Volatile<boolean | undefined>
  /** Token cap for the summary snapshot frozen into the system prompt. */
  injectedSummaryTokens: Volatile<number | undefined>
  /** Inject a session-start-frozen summary snapshot into the system prompt. */
  contextInjectionEnabled: Volatile<boolean | undefined>
  /** Whether the out-of-band work-overview synthesis runs. */
  overviewEnabled: Volatile<boolean | undefined>
}

/** Shape of the extraction-model override as stored in the settings document. */
export interface ExtractionModelConfig {
  provider?: string
  model?: string
  baseURL?: string
  protocol?: string
  apiKey?: string
}

/**
 * The plugin configuration as `apply` receives it: the schema's inferred output.
 *
 * Derived from the schema rather than hand-written, so the two cannot drift. A
 * hand-written `Config` would have to restate every `.volatile()` field as
 * `Volatile<T>` by hand — and the one time that restatement is wrong, the
 * compiler blames the schema instead of the interface, which is the wrong file
 * to be editing.
 */
export type Config = ConfigShape

/**
 * The plugin configuration, minus the live references.
 *
 * `Config`'s live fields are `Volatile<T>` references, so this is what the
 * deploy-time view looks like once those are read: the shape every consumer
 * outside the settings wiring actually wants. Keeping it explicit documents
 * which fields survive a restart.
 */
export interface DeployTimeConfig {
  /** Python-side SQLite database path (expanded by the library). */
  dbPath?: string
  /** Override the interpreter used to spawn `python -m atom_memory.rpc`. */
  pythonBin?: string
  /** Auto-start the bridge on plugin load (deployment-time switch). */
  autostart?: boolean
  /**
   * Output-token cap for one LLM extraction call.
   *
   * The whole JSON payload (including any knowledge `content` body) must fit in
   * this budget: exceeding it truncates the response, and a truncated
   * extraction is discarded rather than persisted. Too small a value therefore
   * silently loses long-form knowledge.
   */
  extractionMaxTokens?: number
  /** Whether the periodic nudge capture (write path) is enabled. */
  nudgeEnabled?: boolean
  /** Minutes between periodic nudge sweeps. */
  nudgeIntervalMinutes?: number
  /** Max facts surfaced to the model per recall tool call. */
  maxRecalledFacts?: number
  /** Estimated token cap for returned summary. */
  summaryTokens?: number
  /**
   * Seconds of quiet after a memory write before a refresh is attempted.
   *
   * The debounce window. Every write pushes the deadline out, so a burst of
   * activity costs exactly one synthesis — at the pause, when the store has
   * stopped changing and the overview will not be immediately obsolete.
   */
  overviewIdleSeconds?: number
  /**
   * Minimum minutes between two overview refreshes. `0` disables the floor.
   *
   * The changelog gate already prevents a refresh that would change nothing;
   * this prevents a *repeated* one, so a long session cannot turn into a stream
   * of completions however much it writes.
   */
  overviewRefreshMinutes?: number
  /** Per-RPC timeout in ms. */
  rpcTimeoutMs?: number
  /**
   * How long a write tool waits for the store's verdict before returning the
   * enqueue receipt.
   *
   * The write pipeline is asynchronous by design, but a *decision* is made
   * synchronously inside it (does this contradict something stored? does the
   * newer claim win?), and a tool that reported only "queued" would hide the
   * decision that matters most. This is a bounded acknowledgement, not a
   * synchronous pipeline: on timeout the caller gets the receipt as before.
   * `0` disables the wait.
   */
  writeAckTimeoutMs?: number
  /**
   * Cosine-distance ceiling for semantic recall.
   *
   * The only filter that can say "nothing here is close" — a rank-based score
   * cannot, because the nearest of twenty bad matches still ranks first. Measured
   * on `BAAI/bge-small-zh-v1.5`: related pairs sit at 0.33–0.54, cross-language
   * related pairs around 0.65, clearly unrelated ones at 0.67–0.85, so the
   * default is a coarse "different topic area" floor rather than a quality bar.
   * Raise it for looser recall, lower it (≈0.60) for a single-language store.
   */
  maxVectorDistance?: number
  /**
   * Fused-relevance floor (0..1). Candidates below it are not returned at all,
   * so "no memory is relevant" is expressible.
   *
   * Only meaningful together with `maxVectorDistance`: on its own, every
   * candidate that reached either top-k clears it, which is the trap a
   * rank-based floor sets. `0` disables it.
   *
   * The default is deliberately non-zero. At `0` the floor never fires and
   * recall always answers with a full `maxRecalledFacts` of rows, which makes a
   * large store indistinguishable from a tiny one and pushes the weakest
   * lexical-only matches into the context budget. `0.15` sits below the
   * single-list band (a fact in one ranking scores 0.5, see
   * `relevance_from_rrf`), so it trims the tail without touching a fact that
   * ranked well on either leg.
   */
  minRelevance?: number
  /**
   * How much deeper than `top_k` each retrieval leg looks before fusion.
   *
   * RRF scores a fact by how well the two rankings agree, so a fact outside
   * both top-k lists is unreachable no matter how strong its importance or
   * recency — it was never fused in the first place. Retrieving deep and
   * trimming after the re-rank is what lets the absolute terms promote a
   * deep-but-relevant fact over a shallow-but-generic one. `1` restores the
   * old "fuse exactly top_k" behaviour.
   */
  candidatePoolMultiplier?: number
  /**
   * Age at which a fact's recency credit halves in the re-rank, in days.
   *
   * Distinct from the reinforcement half-life: this one is how much "recent" is
   * worth, that one is how much reuse is worth.
   */
  recencyHalfLifeDays?: number
  /**
   * Cap on the recency shift, in days. `0` or omitted derives
   * `3 × recencyHalfLifeDays`, which is the shipped ratio.
   */
  recencyReferenceWindowDays?: number
  /**
   * Soft cap on one user's active facts. `0` (the default) is unlimited.
   *
   * When exceeded, the maintenance pass moves the least valuable *unprotected*
   * facts to the archive tier — never deletes them, and never touches facts
   * that are fresh, reinforced, or durable knowledge.
   */
  maxActiveFacts?: number
  /**
   * Hard cap on the number of user-profile rows. `0` disables the cap.
   *
   * The profile is rendered into the session system prompt through `user_md`,
   * so every row is a cost paid on every request: this is what keeps that cost
   * bounded. A write that would exceed it is refused with the count and the
   * limit (editing an existing row is always allowed), rather than silently
   * dropping the oldest entry.
   */
  maxProfileRows?: number
  /**
   * Per-fact ceiling inside one recall result, in estimated tokens.
   *
   * The recall budget is otherwise soft (the first match is always kept so a
   * tiny budget cannot return nothing), which measured at ~60x overshoot for one
   * long knowledge body. An oversized body is shortened to this ceiling, the fact
   * is flagged as truncated, and `memory_get` returns the whole text.
   */
  maxFactTokens?: number
  /**
   * Cosine-distance gate for folding a *reworded* knowledge body into the memory
   * it repeats.
   *
   * The fingerprint half of deduplication (identical content, including for
   * knowledge bodies whose title is derived) is always on. This gate adds the
   * approximate half and only for bodies long enough to be a document; `0`
   * disables it. Keep it tight: a false merge removes a distinct memory from the
   * working set, while a missed merge costs one redundant row.
   */
  dedupMaxDistance?: number
  /**
   * Predicates to treat as *multi-valued*, on top of the store's built-in set.
   *
   * Under a single-valued key a second, different value retires the first (or,
   * inside one batch, is dropped) — which is right for "my job title" and wrong
   * for a claim that is one-to-many however it is worded ("在研课题",
   * "课程大纲编写事项"). The predicates are written by the extractor, so the
   * set cannot be closed in advance; this is where a deployment adds the ones
   * its own memory keeps colliding on. Empty (the default) changes nothing.
   */
  multiValuedPredicates?: string[]
  /**
   * Whether scope-aware memory is on: collect this session's context signals
   * (git root and origin remote, declared package name, working directory) and
   * send them as the `scope_context` payload of every scope-aware RPC call.
   *
   * Deliberately a deploy-time field and *not* in the `atom-memory` settings
   * namespace: it describes the deployment (which checkout this harness serves,
   * and under which tags), not how memory should behave at this moment. It is
   * also fully reversible at the wire level — with it off, every call sends
   * exactly the params it sent before scope awareness existed, and the store
   * behaves globally.
   */
  scopeEnabled?: boolean
  /**
   * Explicit org tag, sent as the `explicit_org` signal.
   *
   * The explicit tags are "the user said so" evidence (reliability 0.95 on the
   * Python side, far above anything inferred from a path), so one is worth
   * setting whenever a deployment serves exactly one of the thing. Like
   * `scopeEnabled` these are composition configuration only and have no
   * runtime/settings counterpart: retagging a deployment is not a memory switch
   * to flip mid-session but a different deployment.
   */
  scopeOrg?: string
  /** Explicit client tag (`explicit_client`), e.g. the customer this harness serves. */
  scopeClient?: string
  /** Explicit project tag (`explicit_project`), for a harness pinned to one project. */
  scopeProject?: string
  /** Explicit series tag (`explicit_series`), e.g. the newsletter a session belongs to. */
  scopeSeries?: string
  /** Explicit phase tag (`explicit_phase`), e.g. `draft`; also sent as the context's phase. */
  scopePhase?: string
}

/**
 * Fields the settings panel may edit at runtime, marked `.volatile()`.
 *
 * This is not decoration: DSH's settings layer projects a plugin's Config into
 * a panel form through `volatileForm(schema)`, which only descends into nodes
 * carrying `meta.volatile`. A Config with no volatile field produces NO form,
 * and `describe()` then drops the plugin from the served namespace list
 * entirely — the browser panel finds no namespace, marks itself unavailable,
 * and every control it guards renders disabled. So a field that belongs in the
 * panel must be volatile, and a field that does not must NOT be (marking
 * `dbPath` volatile would offer a live editor for a value the bridge only reads
 * at spawn time).
 *
 * The split below is exactly "may change mid-session" vs "describes the
 * deployment": the Python interpreter and database path are fixed when the
 * bridge is spawned, while the switches, the injection budget and the
 * extraction-model override are read through getters on every use.
 *
 * No `z<Config>` annotation here, and that is deliberate: the annotation would
 * demand that the schema's OUTPUT equal `Config`, but a volatile node's output
 * is `Volatile<T>` — the very reference `Config` describes. The harness's own
 * plugins (`ui-theme`, `ui-chat`) leave their Config unannotated for this
 * reason, and the runtime shape is taken from the schema's inferred output
 * (see `ConfigShape` / `Config` below) rather than restated by hand.
 */
export const Config = z.object({
  // ---- composition fields: not editable at runtime, so not volatile ----
  dbPath: z.string().default('~/.dsh/atom-memory/memory.db'),
  pythonBin: z.string().default(''),
  autostart: z.boolean().default(true),
  extractionMaxTokens: z.number().default(2048),
  nudgeEnabled: z.boolean().default(true),
  nudgeIntervalMinutes: z.number().default(30),
  maxRecalledFacts: z.number().default(10),
  summaryTokens: z.number().default(1500),
  overviewIdleSeconds: z.number().default(90),
  overviewRefreshMinutes: z.number().default(15),
  rpcTimeoutMs: z.number().default(30_000),
  writeAckTimeoutMs: z.number().default(2500),
  maxVectorDistance: z.number().default(0.70),
  minRelevance: z.number().default(0.15),
  /**
   * How much deeper than `maxRecalledFacts` each retrieval leg looks before
   * fusion. Reciprocal Rank Fusion scores a fact by how well the two rankings
   * agree, so a fact outside both top-k lists cannot be recovered by any
   * ranking term — it was never fused. Fusing only `top_k` per leg therefore
   * makes the result set a function of two rank positions alone: measured on
   * this store, a query's true answer ranked 15th lexically and 11th
   * semantically and was invisible at 1x, while `4` retrieves it. `1` restores
   * the old behaviour at the cost of that recall.
   */
  candidatePoolMultiplier: z.number().step(1).min(1).default(4),
  /**
   * Age at which a fact's recency credit halves *in the re-rank*.
   *
   * How much "recent" is worth — distinct from `reinforce`-side decay, which is
   * how much *reuse* is worth. A fact's age is measured from `last_used_at`
   * where it has been used, so a long-lived fact still in active use is not
   * aged out merely for being old. Lower it for a store whose facts turn over
   * fast; raise it for durable knowledge where age should barely matter.
   */
  recencyHalfLifeDays: z.number().min(0.1).default(30),
  /**
   * Cap on the recency shift, in days. Derived as `3 × recencyHalfLifeDays`
   * when omitted (`0`), which is the shipped ratio.
   *
   * The cap exists so an entirely-old result set still spreads its credits
   * instead of reading as uniformly stale. Keeping it well above the half-life
   * is what stops it flattening genuinely different ages onto one value, so
   * changing it without changing the half-life is rarely what you want.
   */
  recencyReferenceWindowDays: z.number().min(0).default(0),
  maxActiveFacts: z.number().default(0),
  maxProfileRows: z.number().default(50),
  maxFactTokens: z.number().default(600),
  dedupMaxDistance: z.number().default(0.10),
  multiValuedPredicates: z.array(z.string()).default([]),
  scopeEnabled: z.boolean().default(true),
  // Empty means "no tag": an empty signal value is dropped by the payload
  // builder rather than sent, which is what keeps an unconfigured deployment's
  // params identical to a pre-scope one.
  scopeOrg: z.string().default(''),
  scopeClient: z.string().default(''),
  scopeProject: z.string().default(''),
  scopeSeries: z.string().default(''),
  scopePhase: z.string().default(''),

  // ---- live fields: these are what the settings panel edits ----
  extractionModel: z.object({
    provider: z.string().default(''),
    model: z.string().default(''),
    baseURL: z.string().default(''),
    protocol: z.string().default('openai'),
    apiKey: z.string().default(''),
  }).default({ provider: '', model: '', baseURL: '', protocol: 'openai', apiKey: '' }).volatile(),
  captureEnabled: z.boolean().default(true).volatile(),
  llmExtractionEnabled: z.boolean().default(true).volatile(),
  injectedSummaryTokens: z.number().default(DEFAULT_INJECTED_SUMMARY_TOKENS).volatile(),
  contextInjectionEnabled: z.boolean().default(true).volatile(),
  overviewEnabled: z.boolean().default(true).volatile(),
})

/**
 * The schema's inferred output — what `apply` actually receives.
 *
 * Named separately from the exported `Config` because `src/index.ts` re-exports
 * `Config` as the plugin's schema, which is what the Loader reads.
 */
export type ConfigShape = ReturnType<typeof Config>

