import z from "@deepseek-ai/schemastery";
import { Context } from "@deepseek-ai/cordis";
//#region src/config.d.ts
interface Config {
  /** Python-side SQLite database path (expanded by the library). */
  dbPath?: string;
  /** Override the interpreter used to spawn `python -m atom_memory.rpc`. */
  pythonBin?: string;
  /** Auto-start the bridge on plugin load (deployment-time switch). */
  autostart?: boolean;
  /** Master memory switch: when false the plugin is inert (no capture/context/tools). */
  enabled?: boolean;
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
  /** Whether the pre-compression rescue hook is enabled. */
  preCompressionCapture?: boolean;
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
   * that are fresh, reinforced, durable knowledge, or backing a pinned profile
   * row.
   */
  maxActiveFacts?: number;
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
}
declare const Config: z<Config>;
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
declare function apply(ctx: Context, config: Config): void;
//#endregion
export { Config, apply, inject, name, retryDelayMs };