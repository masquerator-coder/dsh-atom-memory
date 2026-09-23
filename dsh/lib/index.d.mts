import z from "@deepseek-ai/schemastery";
import "@deepseek-ai/dsh-llm";
import { Context } from "@deepseek-ai/cordis";
//#region src/config.d.ts
/**
 * The plugin configuration as `apply` receives it: the schema's inferred output.
 *
 * Derived from the schema rather than hand-written, so the two cannot drift. A
 * hand-written `Config` would have to restate every `.volatile()` field as
 * `Volatile<T>` by hand — and the one time that restatement is wrong, the
 * compiler blames the schema instead of the interface, which is the wrong file
 * to be editing.
 */
type Config = ConfigShape;
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
declare const Config: z<Schemastery.ObjectS<NoInfer<{
  dbPath: z<string, string, "defined">;
  pythonBin: z<string, string, "defined">;
  autostart: z<boolean, boolean, "defined">;
  extractionMaxTokens: z<number, number, "defined">;
  nudgeEnabled: z<boolean, boolean, "defined">;
  nudgeIntervalMinutes: z<number, number, "defined">;
  maxRecalledFacts: z<number, number, "defined">;
  summaryTokens: z<number, number, "defined">;
  overviewIdleSeconds: z<number, number, "defined">;
  overviewRefreshMinutes: z<number, number, "defined">;
  rpcTimeoutMs: z<number, number, "defined">;
  writeAckTimeoutMs: z<number, number, "defined">;
  maxVectorDistance: z<number, number, "defined">;
  minRelevance: z<number, number, "defined">;
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
  candidatePoolMultiplier: z<number, number, "defined">;
  /**
   * Age at which a fact's recency credit halves *in the re-rank*.
   *
   * How much "recent" is worth — distinct from `reinforce`-side decay, which is
   * how much *reuse* is worth. A fact's age is measured from `last_used_at`
   * where it has been used, so a long-lived fact still in active use is not
   * aged out merely for being old. Lower it for a store whose facts turn over
   * fast; raise it for durable knowledge where age should barely matter.
   */
  recencyHalfLifeDays: z<number, number, "defined">;
  /**
   * Cap on the recency shift, in days. Derived as `3 × recencyHalfLifeDays`
   * when omitted (`0`), which is the shipped ratio.
   *
   * The cap exists so an entirely-old result set still spreads its credits
   * instead of reading as uniformly stale. Keeping it well above the half-life
   * is what stops it flattening genuinely different ages onto one value, so
   * changing it without changing the half-life is rarely what you want.
   */
  recencyReferenceWindowDays: z<number, number, "defined">;
  maxActiveFacts: z<number, number, "defined">;
  maxProfileRows: z<number, number, "defined">;
  maxFactTokens: z<number, number, "defined">;
  dedupMaxDistance: z<number, number, "defined">;
  multiValuedPredicates: z<string[], string[], "defined">;
  scopeEnabled: z<boolean, boolean, "defined">;
  scopeOrg: z<string, string, "defined">;
  scopeClient: z<string, string, "defined">;
  scopeProject: z<string, string, "defined">;
  scopeSeries: z<string, string, "defined">;
  scopePhase: z<string, string, "defined">;
  extractionModel: z<NoInfer<Schemastery.ObjectS<NoInfer<{
    provider: z<string, string, "defined">;
    model: z<string, string, "defined">;
    baseURL: z<string, string, "defined">;
    protocol: z<string, string, "defined">;
    apiKey: z<string, string, "defined">;
  }>>>, NoInfer<Schemastery.ObjectT<NoInfer<{
    provider: z<string, string, "defined">;
    model: z<string, string, "defined">;
    baseURL: z<string, string, "defined">;
    protocol: z<string, string, "defined">;
    apiKey: z<string, string, "defined">;
  }>>>, "volatile-defined">;
  captureEnabled: z<boolean, boolean, "volatile-defined">;
  llmExtractionEnabled: z<boolean, boolean, "volatile-defined">;
  injectedSummaryTokens: z<number, number, "volatile-defined">;
  contextInjectionEnabled: z<boolean, boolean, "volatile-defined">;
  overviewEnabled: z<boolean, boolean, "volatile-defined">;
}>>, Schemastery.ObjectT<NoInfer<{
  dbPath: z<string, string, "defined">;
  pythonBin: z<string, string, "defined">;
  autostart: z<boolean, boolean, "defined">;
  extractionMaxTokens: z<number, number, "defined">;
  nudgeEnabled: z<boolean, boolean, "defined">;
  nudgeIntervalMinutes: z<number, number, "defined">;
  maxRecalledFacts: z<number, number, "defined">;
  summaryTokens: z<number, number, "defined">;
  overviewIdleSeconds: z<number, number, "defined">;
  overviewRefreshMinutes: z<number, number, "defined">;
  rpcTimeoutMs: z<number, number, "defined">;
  writeAckTimeoutMs: z<number, number, "defined">;
  maxVectorDistance: z<number, number, "defined">;
  minRelevance: z<number, number, "defined">;
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
  candidatePoolMultiplier: z<number, number, "defined">;
  /**
   * Age at which a fact's recency credit halves *in the re-rank*.
   *
   * How much "recent" is worth — distinct from `reinforce`-side decay, which is
   * how much *reuse* is worth. A fact's age is measured from `last_used_at`
   * where it has been used, so a long-lived fact still in active use is not
   * aged out merely for being old. Lower it for a store whose facts turn over
   * fast; raise it for durable knowledge where age should barely matter.
   */
  recencyHalfLifeDays: z<number, number, "defined">;
  /**
   * Cap on the recency shift, in days. Derived as `3 × recencyHalfLifeDays`
   * when omitted (`0`), which is the shipped ratio.
   *
   * The cap exists so an entirely-old result set still spreads its credits
   * instead of reading as uniformly stale. Keeping it well above the half-life
   * is what stops it flattening genuinely different ages onto one value, so
   * changing it without changing the half-life is rarely what you want.
   */
  recencyReferenceWindowDays: z<number, number, "defined">;
  maxActiveFacts: z<number, number, "defined">;
  maxProfileRows: z<number, number, "defined">;
  maxFactTokens: z<number, number, "defined">;
  dedupMaxDistance: z<number, number, "defined">;
  multiValuedPredicates: z<string[], string[], "defined">;
  scopeEnabled: z<boolean, boolean, "defined">;
  scopeOrg: z<string, string, "defined">;
  scopeClient: z<string, string, "defined">;
  scopeProject: z<string, string, "defined">;
  scopeSeries: z<string, string, "defined">;
  scopePhase: z<string, string, "defined">;
  extractionModel: z<NoInfer<Schemastery.ObjectS<NoInfer<{
    provider: z<string, string, "defined">;
    model: z<string, string, "defined">;
    baseURL: z<string, string, "defined">;
    protocol: z<string, string, "defined">;
    apiKey: z<string, string, "defined">;
  }>>>, NoInfer<Schemastery.ObjectT<NoInfer<{
    provider: z<string, string, "defined">;
    model: z<string, string, "defined">;
    baseURL: z<string, string, "defined">;
    protocol: z<string, string, "defined">;
    apiKey: z<string, string, "defined">;
  }>>>, "volatile-defined">;
  captureEnabled: z<boolean, boolean, "volatile-defined">;
  llmExtractionEnabled: z<boolean, boolean, "volatile-defined">;
  injectedSummaryTokens: z<number, number, "volatile-defined">;
  contextInjectionEnabled: z<boolean, boolean, "volatile-defined">;
  overviewEnabled: z<boolean, boolean, "volatile-defined">;
}>>, "plain">;
/**
 * The schema's inferred output — what `apply` actually receives.
 *
 * Named separately from the exported `Config` because `src/index.ts` re-exports
 * `Config` as the plugin's schema, which is what the Loader reads.
 */
type ConfigShape = ReturnType<typeof Config>;
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
declare function buildStartParams(config: ConfigShape): Record<string, unknown>;
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
declare function apply(ctx: Context, config: ConfigShape): void;
//#endregion
export { CaptureWiring, Config, apply, buildStartParams, createCapture, inject, name, retryDelayMs };