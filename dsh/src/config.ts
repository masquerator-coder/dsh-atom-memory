/**
 * Plugin configuration (schemastery). See the repo design doc for the
 * rationale of each field. All fields are optional with safe defaults so the
 * plugin behaves sanely when only `dbPath` is provided.
 *
 * @module dsh-atom-memory/config
 */
import z from '@deepseek-ai/schemastery'
import { DEFAULT_INJECTED_SUMMARY_TOKENS } from './injection-budget.ts'

export interface Config {
  /** Python-side SQLite database path (expanded by the library). */
  dbPath?: string
  /** Override the interpreter used to spawn `python -m atom_memory.rpc`. */
  pythonBin?: string
  /** Auto-start the bridge on plugin load (deployment-time switch). */
  autostart?: boolean
  /** Master memory switch: when false the plugin is inert (no capture/context/tools). */
  enabled?: boolean
  /** Manual LLM extraction model override; omit or leave provider empty to follow dsh default. */
  extractionModel?: {
    provider?: string
    model?: string
    /** Custom OpenAI-compatible endpoint base URL. When set, the extractor calls it directly. */
    baseURL?: string
    /** Wire protocol the endpoint speaks (only `openai` supported). */
    protocol?: string
    /** API key for a custom endpoint (plaintext). */
    apiKey?: string
  }
  /** Whether the session/durable capture hooks (turn/end, user/message) run. */
  captureEnabled?: boolean
  /** Whether the LLM-first extractor is wired to the dsh default model. */
  llmExtractionEnabled?: boolean
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
   * Token cap for the summary snapshot frozen into the system prompt.
   *
   * Deliberately separate from (and smaller than) `summaryTokens`: the
   * injected text is paid for on every request of a session and is rendered at
   * the compact depth, while the tool/settings view returns the full detail
   * list.
   *
   * This is only the *seed* for the live value: the settings panel owns it at
   * runtime (`atom-memory` → `injectedSummaryTokens`), and a change there
   * applies to every session that has not frozen its snapshot yet.
   */
  injectedSummaryTokens?: number
  /** Inject a session-start-frozen summary snapshot into the system prompt. */
  contextInjectionEnabled?: boolean
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
   */
  minRelevance?: number
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

export const Config: z<Config> = z.object({
  dbPath: z.string().default('~/.dsh/atom-memory/memory.db'),
  pythonBin: z.string().default(''),
  autostart: z.boolean().default(true),
  enabled: z.boolean().default(true),
  extractionModel: z.object({
    provider: z.string().default(''),
    model: z.string().default(''),
    baseURL: z.string().default(''),
    protocol: z.string().default('openai'),
    apiKey: z.string().default(''),
  }).default({ provider: '', model: '', baseURL: '', protocol: 'openai', apiKey: '' }),
  captureEnabled: z.boolean().default(true),
  llmExtractionEnabled: z.boolean().default(true),
  extractionMaxTokens: z.number().default(2048),
  nudgeEnabled: z.boolean().default(true),
  nudgeIntervalMinutes: z.number().default(30),
  maxRecalledFacts: z.number().default(10),
  summaryTokens: z.number().default(1500),
  injectedSummaryTokens: z.number().default(DEFAULT_INJECTED_SUMMARY_TOKENS),
  contextInjectionEnabled: z.boolean().default(true),
  rpcTimeoutMs: z.number().default(30_000),
  writeAckTimeoutMs: z.number().default(2500),
  maxVectorDistance: z.number().default(0.70),
  minRelevance: z.number().default(0),
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
})

