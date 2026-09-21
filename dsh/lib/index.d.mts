import z from "@deepseek-ai/schemastery";
import "@deepseek-ai/dsh-llm";
import { Context } from "@deepseek-ai/cordis";
//#region src/config.d.ts
interface Config {
  /** Python-side SQLite database path (expanded by the library). */
  dbPath?: string;
  /** Override the interpreter used to spawn `python -m atom_memory.rpc`. */
  pythonBin?: string;
  /** Auto-start the bridge on plugin load (deployment-time switch). */
  autostart?: boolean;
  /** Manual LLM extraction model override; omit or leave provider empty to follow dsh default. */
  extractionModel?: {
    provider?: string;
    model?: string;
    /** Custom OpenAI-compatible endpoint base URL. When set, the extractor calls it directly. */
    baseURL?: string;
    /** Wire protocol the endpoint speaks (only `openai` supported). */
    protocol?: string;
    /** API key for a custom endpoint (plaintext). */
    apiKey?: string;
  };
  /** Whether the session/durable capture hooks (turn/end, user/message) run. */
  captureEnabled?: boolean;
  /** Whether the LLM-first extractor is wired to the dsh default model. */
  llmExtractionEnabled?: boolean;
  /**
   * Output-token cap for one LLM extraction call.
   *
   * The whole JSON payload (including any knowledge `content` body) must fit in
   * this budget: exceeding it truncates the response, and a truncated
   * extraction is discarded rather than persisted. Too small a value therefore
   * silently loses long-form knowledge.
   */
  extractionMaxTokens?: number;
  /** Whether the periodic nudge capture (write path) is enabled. */
  nudgeEnabled?: boolean;
  /** Minutes between periodic nudge sweeps. */
  nudgeIntervalMinutes?: number;
  /** Max facts surfaced to the model per recall tool call. */
  maxRecalledFacts?: number;
  /** Estimated token cap for returned summary. */
  summaryTokens?: number;
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
  injectedSummaryTokens?: number;
  /** Inject a session-start-frozen summary snapshot into the system prompt. */
  contextInjectionEnabled?: boolean;
  /**
   * Whether the out-of-band work-overview synthesis runs.
   *
   * The injected snapshot leads with a model-written narrative of what has been
   * worked on. Writing it costs a completion, so it is done ahead of time in a
   * quiet moment rather than while a prompt is frozen; this switch turns that
   * background job off. The snapshot itself is unaffected — with this off it
   * leads with the deterministic overview instead.
   */
  overviewEnabled?: boolean;
  /**
   * Seconds of quiet after a memory write before a refresh is attempted.
   *
   * The debounce window. Every write pushes the deadline out, so a burst of
   * activity costs exactly one synthesis — at the pause, when the store has
   * stopped changing and the overview will not be immediately obsolete.
   */
  overviewIdleSeconds?: number;
  /**
   * Minimum minutes between two overview refreshes. `0` disables the floor.
   *
   * The changelog gate already prevents a refresh that would change nothing;
   * this prevents a *repeated* one, so a long session cannot turn into a stream
   * of completions however much it writes.
   */
  overviewRefreshMinutes?: number;
  /** Per-RPC timeout in ms. */
  rpcTimeoutMs?: number;
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
  writeAckTimeoutMs?: number;
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
  maxVectorDistance?: number;
  /**
   * Fused-relevance floor (0..1). Candidates below it are not returned at all,
   * so "no memory is relevant" is expressible.
   *
   * Only meaningful together with `maxVectorDistance`: on its own, every
   * candidate that reached either top-k clears it, which is the trap a
   * rank-based floor sets. `0` disables it.
   */
  minRelevance?: number;
  /**
   * Soft cap on one user's active facts. `0` (the default) is unlimited.
   *
   * When exceeded, the maintenance pass moves the least valuable *unprotected*
   * facts to the archive tier — never deletes them, and never touches facts
   * that are fresh, reinforced, or durable knowledge.
   */
  maxActiveFacts?: number;
  /**
   * Hard cap on the number of user-profile rows. `0` disables the cap.
   *
   * The profile is rendered into the session system prompt through `user_md`,
   * so every row is a cost paid on every request: this is what keeps that cost
   * bounded. A write that would exceed it is refused with the count and the
   * limit (editing an existing row is always allowed), rather than silently
   * dropping the oldest entry.
   */
  maxProfileRows?: number;
  /**
   * Per-fact ceiling inside one recall result, in estimated tokens.
   *
   * The recall budget is otherwise soft (the first match is always kept so a
   * tiny budget cannot return nothing), which measured at ~60x overshoot for one
   * long knowledge body. An oversized body is shortened to this ceiling, the fact
   * is flagged as truncated, and `memory_get` returns the whole text.
   */
  maxFactTokens?: number;
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
  dedupMaxDistance?: number;
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
  multiValuedPredicates?: string[];
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
  scopeEnabled?: boolean;
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
  scopeOrg?: string;
  /** Explicit client tag (`explicit_client`), e.g. the customer this harness serves. */
  scopeClient?: string;
  /** Explicit project tag (`explicit_project`), for a harness pinned to one project. */
  scopeProject?: string;
  /** Explicit series tag (`explicit_series`), e.g. the newsletter a session belongs to. */
  scopeSeries?: string;
  /** Explicit phase tag (`explicit_phase`), e.g. `draft`; also sent as the context's phase. */
  scopePhase?: string;
}
declare const Config: z<Config>;
//#endregion
//#region src/llm-extractor.d.ts
/**
 * One ``(key, value)`` condition: *when* a claim holds.
 *
 * Field names are the Python side's verbatim — a candidate object *is* the wire
 * payload of ``persist_candidates``, so a renamed key would be accepted by the
 * JSON parser and then silently ignored by the store.
 */
interface ExtractedCondition {
  key: string;
  value: string;
}
/** One typed candidate matching the Python ``persist_candidates`` wire shape. */
interface ExtractedCandidate {
  subject: string;
  predicate: string;
  object: string;
  type?: string;
  content?: string;
  qualifiers?: Record<string, unknown>;
  confidence?: number;
  importance?: number;
  /**
   * Conditions the claim holds *under* (language, doc_type, audience, industry,
   * stage, tool, vcs): a fact that is only true for a proposal in Chinese must
   * not outrank the same claim in the other context.
   */
  conditions?: ExtractedCondition[];
  /**
   * Free-text hint about *where* the fact belongs.
   *
   * Named exactly as the Python candidate field reads it. It is only a hint: the
   * store treats it as a low-reliability content anchor (0.25) that can
   * corroborate a resolution or accumulate in the candidate queue, but can never
   * bind a scope on its own.
   */
  scope_hint?: string;
  /**
   * What the fact is *about*, as canonical names (``["teaching/ds", "programming"]``).
   *
   * Unlike ``scope_hint`` this is acted on: the store resolves each name against
   * the user's topic vocabulary, and a name the vocabulary does not hold becomes
   * its nearest registered ancestor plus a queue entry for the user to decide on.
   * The vocabulary lives in the Python store, so the prompt asks for these only
   * as plain words; the store owns what they resolve to.
   */
  domain_hints?: string[];
  /**
   * Which of ``domain_hints`` is the main topic. Moved to the front before the
   * cap is applied, so the model may list it anywhere.
   */
  primary_domain?: string;
}
/** The extraction callable signature the capture/tool layer uses. */
type ExtractFn = (text: string) => Promise<ExtractedCandidate[]>;
//#endregion
//#region src/scope.d.ts
/**
 * The `scope_context` wire payload.
 *
 * Field names are the contract's verbatim (snake_case): this object is
 * serialized straight into the RPC params, so a renamed key would be silently
 * ignored by the Python side rather than reported.
 */
interface ScopeContextPayload {
  /** `signal_type` -> raw value. */
  signals?: Record<string, string>;
  /** Conditions of the current context, e.g. `{doc_type: 'proposal'}`. */
  conditions?: Record<string, string>;
  /** Current phase (also a scope type of its own). */
  phase?: string;
  /** Free-text hint about where the work belongs; the weakest evidence. */
  scope_hint?: string;
}
//#endregion
//#region src/index.d.ts
declare const name = "dsh-atom-memory";
/**
 * Required services. `tools` and `systemPrompt` are the only hard
 * dependencies — matching the reference dsh-memory plugin. `llm`,
 * `agentDefaultModel` and `settings` are read via `ctx.get`, never injected
 * (they are optional, model-versioned, or deployment-determined services).
 */
declare const inject: readonly ["tools", "systemPrompt"];
/**
 * Delay before start attempt `attempt` (1-based), doubling each time.
 *
 * Exported so the backoff the README promises is testable without spawning a
 * bridge: the delay is the only part of the retry policy that is pure.
 *
 * @param attempt - Which attempt is about to run (1 = the first retry).
 * @returns Milliseconds to wait, capped at {@link MAX_RETRY_MS}.
 */
declare function retryDelayMs(attempt: number): number;
/**
 * Start params sent to the Python bridge.
 *
 * These are `MemConfig` field names verbatim: the RPC `start` handler builds
 * the config from them, so a typo becomes an "invalid start params" error rather
 * than a silently ignored setting.
 *
 * Exported for the same reason {@link retryDelayMs} is: the params *are* the
 * wire contract, and a field that is never sent is a setting that silently does
 * nothing.
 */
declare function buildStartParams(config: Config): Record<string, unknown>;
/** Everything the ingestion point needs, so it can be exercised without a child. */
interface CaptureWiring {
  /**
   * Whether the Python bridge is up. A down bridge means *no* RPC at all rather
   * than a failed one: the message stays uncaptured and the nudge retries it.
   */
  isReady: () => boolean;
  /** LLM-first extractor; absent means the rule engine is the only extractor. */
  extract?: ExtractFn;
  /** Send one RPC to the memory store. */
  call: (method: string, params: Record<string, unknown>) => Promise<unknown>;
  /**
   * Scope payload for a working directory (`undefined` = send nothing, which is
   * what a scope-disabled deployment does on every call).
   */
  scopeContextAt: (cwd?: string) => ScopeContextPayload | undefined;
}
/**
 * Build the single ingestion point: LLM-first candidates go to
 * `persist_candidates`, and the rule path (`add`) takes anything extraction did
 * not turn into facts. Both carry the session's scope context, so an automatic
 * capture lands in the same scope a tool call from that session would.
 *
 * Extracted from `apply` (like {@link retryDelayMs}) so the *wire shape* of an
 * automatic capture is testable without spawning a Python child: it is the path
 * every user message takes, and the tools reach the same two RPCs by a route
 * that would not catch a mistake here.
 *
 * @param deps - Gates, extractor, RPC sink and scope-context builder.
 * @returns The capture function the hooks call, `(text, sessionId, cwd?)`.
 */
declare function createCapture(deps: CaptureWiring): (text: string, sessionId: string, cwd?: string) => Promise<void>;
declare function apply(ctx: Context, config: Config): void;
//#endregion
export { CaptureWiring, Config, apply, buildStartParams, createCapture, inject, name, retryDelayMs };