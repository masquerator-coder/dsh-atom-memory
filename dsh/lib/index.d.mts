/**
 * dsh-atom-memory — dsh-side integration for the Python memory library.
 *
 * A Cordis plugin that:
 *  - spawns and manages the `atom_memory.rpc` Python child process,
 *  - exposes the `memory_*` tools (add/replace/recall/summary/snapshot/
 *    summary_detail/forget/user_md/stats),
 *  - wires an LLM-first extractor that uses the dsh default model and ships
 *    typed candidates to Python for persistence (rules remain the fallback),
 *  - collects each session's context signals (working directory, git root and
 *    origin remote, declared package) and sends them as `scope_context` on
 *    every read, write and prompt freeze, so memory lands in the right scope
 *    without anyone tagging it by hand,
 *  - registers durable capture hooks (per-message, pre-compression rescue,
 *    periodic nudge) so conversation turns into memory automatically,
 *  - injects a system-prompt awareness section plus a per-session memory
 *    snapshot frozen at the session's first prompt assembly, rendered through
 *    the memory-data fence so stored content can never act as prompt structure.
 *
 * It never modifies dsh source and never imports the Python library — all
 * memory lives in the isolated child process, reached over the NDJSON bridge.
 *
 * Two operational properties this module is responsible for, because they are
 * about *when* things happen rather than what they compute:
 *
 *  - **Every switch is read when it is used.** The live toggles (capture,
 *    context injection, LLM extraction) reach their consumers as getters, so
 *    flipping one in the settings panel takes effect on the next event instead
 *    of on the next restart. Whether the plugin runs at all is dsh's own plugin
 *    switch: this module has no master switch of its own.
 *  - **The Python side is probed before the bridge is trusted.** A child that
 *    cannot import `atom_memory` exits immediately; without a preflight the
 *    plugin would look healthy and fail every call. A permanent failure (wrong
 *    interpreter, library not installed) is reported once with an actionable
 *    message instead of being retried into silence.
 *
 * @module dsh-atom-memory/index
 */
import type { Context } from '@deepseek-ai/cordis';
import { Config, type Config as ConfigShape } from './config.ts';
import { type ExtractFn } from './llm-extractor.ts';
import { type ScopeContextPayload } from './scope.ts';
export declare const name = "dsh-atom-memory";
/**
 * Required services. `tools` and `systemPrompt` are the only hard
 * dependencies — matching the reference dsh-memory plugin. `llm`,
 * `agentDefaultModel` and `settings` are read via `ctx.get`, never injected
 * (they are optional, model-versioned, or deployment-determined services).
 */
export declare const inject: readonly ["tools", "systemPrompt"];
export { Config };
/**
 * Delay before start attempt `attempt` (1-based), doubling each time.
 *
 * Exported so the backoff the README promises is testable without spawning a
 * bridge: the delay is the only part of the retry policy that is pure.
 *
 * @param attempt - Which attempt is about to run (1 = the first retry).
 * @returns Milliseconds to wait, capped at {@link MAX_RETRY_MS}.
 */
export declare function retryDelayMs(attempt: number): number;
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
export declare function buildStartParams(config: ConfigShape): Record<string, unknown>;
/** Everything the ingestion point needs, so it can be exercised without a child. */
export interface CaptureWiring {
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
export declare function createCapture(deps: CaptureWiring): (text: string, sessionId: string, cwd?: string) => Promise<void>;
export declare function apply(ctx: Context, config: ConfigShape): void;
