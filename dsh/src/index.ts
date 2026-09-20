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
 *    of on the next restart.
 *  - **The Python side is probed before the bridge is trusted.** A child that
 *    cannot import `atom_memory` exits immediately; without a preflight the
 *    plugin would look healthy and fail every call. A permanent failure (wrong
 *    interpreter, library not installed) is reported once with an actionable
 *    message instead of being retried into silence.
 *
 * @module dsh-atom-memory/index
 */
import type { Context } from '@deepseek-ai/cordis'
import z from '@deepseek-ai/schemastery'
import { Config, type Config as ConfigShape } from './config.ts'
import {
  DEFAULT_INJECTED_SUMMARY_TOKENS,
  clampInjectedSummaryTokens,
} from './injection-budget.ts'
import { PythonBridge, defaultSpawn } from './bridge.ts'
import { registerMemoryTools } from './tools.ts'
import { registerMemoryContext } from './context.ts'
import { registerCapture } from './capture.ts'
import { buildLlmCompleter, buildLlmExtractor, type ExtractFn } from './llm-extractor.ts'
import { checkPythonSide, type PreflightResult } from './preflight.ts'
import {
  scopeContextForCwd,
  sessionCwdOf,
  type ScopeContextPayload,
  type SessionCwdSource,
} from './scope.ts'
import {
  createRuntime, SETTINGS_NAMESPACE, type LiveRuntime, Runtime,
} from './runtime.ts'
import { AtomMemoryController } from './controller.ts'

export const name = 'dsh-atom-memory'
/**
 * Required services. `tools` and `systemPrompt` are the only hard
 * dependencies — matching the reference dsh-memory plugin. `llm`,
 * `agentDefaultModel` and `settings` are read via `ctx.get`, never injected
 * (they are optional, model-versioned, or deployment-determined services).
 */
export const inject = ['tools', 'systemPrompt'] as const

export { Config }

/** Fallback user/session scope for a single-user local harness. */
const FALLBACK_SCOPE = 'global'

/** Start attempts before the bridge is declared offline. */
const MAX_START_ATTEMPTS = 3

/** First retry delay, and the ceiling exponential backoff climbs to. */
const BASE_RETRY_MS = 1_000
const MAX_RETRY_MS = 15_000

/**
 * Delay before start attempt `attempt` (1-based), doubling each time.
 *
 * Exported so the backoff the README promises is testable without spawning a
 * bridge: the delay is the only part of the retry policy that is pure.
 *
 * @param attempt - Which attempt is about to run (1 = the first retry).
 * @returns Milliseconds to wait, capped at {@link MAX_RETRY_MS}.
 */
export function retryDelayMs(attempt: number): number {
  return Math.min(BASE_RETRY_MS * 2 ** (Math.max(1, attempt) - 1), MAX_RETRY_MS)
}

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
export function buildStartParams(config: ConfigShape): Record<string, unknown> {
  const params: Record<string, unknown> = {
    db_path: config.dbPath ?? '~/.dsh/atom-memory/memory.db',
    worker_poll_interval_sec: 0.5,
    max_retries: 3,
  }
  // Ranking gates. The distance gate is the one filter that can say "nothing
  // here is relevant" — a rank-based score cannot, which is why the ceiling and
  // the floor travel together.
  if (config.maxVectorDistance !== undefined) {
    params.max_vector_distance = config.maxVectorDistance
  }
  if (config.minRelevance !== undefined) {
    params.min_relevance = config.minRelevance
  }
  if (config.maxActiveFacts !== undefined) {
    params.max_active_facts = config.maxActiveFacts
  }
  if (config.maxProfileRows !== undefined) {
    params.max_profile_rows = config.maxProfileRows
  }
  if (config.maxFactTokens !== undefined) {
    params.max_fact_tokens = config.maxFactTokens
  }
  if (config.dedupMaxDistance !== undefined) {
    params.dedup_max_distance = config.dedupMaxDistance
  }
  if (config.writeAckTimeoutMs !== undefined) {
    params.write_ack_timeout_ms = config.writeAckTimeoutMs
  }
  // Sent only when non-empty: an unconfigured deployment's start params stay
  // byte-identical to what they were before this field existed.
  if (config.multiValuedPredicates !== undefined && config.multiValuedPredicates.length > 0) {
    params.multi_valued_predicates = config.multiValuedPredicates
  }
  return params
}

/**
 * Seed the live runtime from the composition config, applying defaults.
 * @param config - the validated composition entry.
 */
function seedRuntime(config: ConfigShape): LiveRuntime {
  return createRuntime({
    enabled: config.enabled !== false,
    captureEnabled: config.captureEnabled !== false,
    llmExtractionEnabled: config.llmExtractionEnabled !== false,
    contextInjectionEnabled: config.contextInjectionEnabled !== false,
    injectedSummaryTokens: config.injectedSummaryTokens,
    extractionModel: config.extractionModel,
  })
}

/** Everything the ingestion point needs, so it can be exercised without a child. */
export interface CaptureWiring {
  /** Master/durability gate: false makes the capture a no-op. */
  isEnabled: () => boolean
  /**
   * Whether the Python bridge is up. A down bridge means *no* RPC at all rather
   * than a failed one: the message stays uncaptured and the nudge retries it.
   */
  isReady: () => boolean
  /** LLM-first extractor; absent means the rule engine is the only extractor. */
  extract?: ExtractFn
  /** Send one RPC to the memory store. */
  call: (method: string, params: Record<string, unknown>) => Promise<unknown>
  /**
   * Scope payload for a working directory (`undefined` = send nothing, which is
   * what a scope-disabled deployment does on every call).
   */
  scopeContextAt: (cwd?: string) => ScopeContextPayload | undefined
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
export function createCapture(
  deps: CaptureWiring,
): (text: string, sessionId: string, cwd?: string) => Promise<void> {
  return async (text: string, sessionId: string, cwd?: string): Promise<void> => {
    if (!deps.isEnabled()) return
    // Built once per capture and shared by both paths: the payload answers
    // "where is this session", which cannot differ between them.
    const scope = deps.scopeContextAt(cwd)
    const scoped = scope === undefined ? {} : { scope_context: scope }
    if (deps.isReady() && deps.extract !== undefined) {
      try {
        const candidates = await deps.extract(text)
        if (candidates.length > 0) {
          await deps.call('persist_candidates', {
            user_id: FALLBACK_SCOPE,
            session_id: sessionId,
            turn_id: 0,
            candidates,
            ...scoped,
          })
          return
        }
      } catch {
        /* fall through to rule extraction on LLM failure */
      }
    }
    if (deps.isReady()) {
      await deps.call('add', {
        user_id: FALLBACK_SCOPE,
        session_id: sessionId,
        text,
        turn_id: 0,
        ...scoped,
      })
    }
  }
}

export function apply(ctx: Context, config: ConfigShape): void {
  const runtime = new Runtime(seedRuntime(config))

  let startTimer: ReturnType<typeof setTimeout> | undefined

  /**
   * Bridge lifecycle state. `preflight` is memoised: the interpreter's ability
   * to import the library does not change between two retries a second apart,
   * and re-probing it would add a subprocess spawn to every retry.
   */
  const state = {
    value: false,
    error: undefined as string | undefined,
    attempt: 0,
    preflight: undefined as PreflightResult | undefined,
  }

  const bridge = new PythonBridge({
    spawnProcess: () => defaultSpawn(config.pythonBin),
    timeoutMs: config.rpcTimeoutMs,
    onEvent: (evt) => {
      ctx.logger(`[atom-memory] ${evt.evt as string} ${evt.candidate_id as string ?? ''}`.trim())
    },
    onLog: (msg) => ctx.logger(`[atom-memory] ${msg}`),
    // Runtime crash after a healthy start: try to bring memory back with a
    // fresh attempt budget (the budget is reset on every successful start).
    onExit: () => {
      state.value = false
      state.attempt = 0
      if (config.autostart !== false && runtime.isEnabled()) void tryStart()
    },
  })

  // All registration is reversible: the child process is killed on unload.
  const lifecycleDisposers: Array<() => void> = []
  lifecycleDisposers.push(() => { void bridge.dispose() })
  lifecycleDisposers.push(() => { if (startTimer !== undefined) { clearTimeout(startTimer); startTimer = undefined } })
  for (const d of lifecycleDisposers) ctx.effect(() => d)

  const tryStart = async (): Promise<void> => {
    if (state.value) return
    if (state.attempt >= MAX_START_ATTEMPTS) {
      ctx.logger('[atom-memory] python bridge failed to (re)start; memory offline')
      return
    }
    if (state.preflight === undefined) {
      state.preflight = await checkPythonSide(config.pythonBin)
      if (!state.preflight.ok) {
        state.error =
          `python side unavailable (${state.preflight.bin}): ${state.preflight.detail}`
        ctx.logger(`[atom-memory] ${state.error}`)
        if (state.preflight.permanent) {
          ctx.logger(
            '[atom-memory] not retrying: install the library into that interpreter '
            + '(`pip install -e .`) or point `pythonBin` at one that has it',
          )
          return
        }
      }
    }
    state.attempt += 1
    try {
      await bridge.start(buildStartParams(config), undefined)
      state.value = true
      // A healthy start resets the budget, so a later runtime crash gets a
      // fresh set of attempts instead of being permanently blocked.
      state.attempt = 0
      state.error = undefined
      const health = await bridge.healthDetail().catch(() => undefined)
      const indexOk = (health?.index as { ok?: boolean } | undefined)?.ok
      ctx.logger(
        `[atom-memory] bridge ready (${(config.dbPath ?? '').trim() || 'db'}`
        + `${indexOk === false ? ', indexes inconsistent → will self-repair' : ''})`,
      )
    } catch (err) {
      state.value = false
      state.error = (err as Error)?.message ?? String(err)
      // Exponential backoff, which is what the README has always claimed
      // ("retried at most three times with backoff") while the code retried on a
      // fixed 1s timer: that hammers an interpreter which is merely slow to warm
      // up, and a flat delay tells a log reader nothing about how many attempts
      // have already gone by. The budget itself is unchanged.
      startTimer = setTimeout(() => { void tryStart() }, retryDelayMs(state.attempt))
    }
  }
  if (config.autostart !== false) void tryStart()

  // LLM-first extraction (optional): prefers a manual extractionModel override,
  // else follows the dsh default model. Both the master switch and the
  // extraction switch are read per call, so the panel's toggles apply
  // immediately in either direction.
  const llmEnabled = () =>
    runtime.isEnabled() && runtime.get().llmExtractionEnabled !== false
  const extract: ExtractFn | undefined = buildLlmExtractor(ctx, {
    maxTokens: config.extractionMaxTokens ?? 2048,
    modelOverride: () => runtime.get().extractionModel,
    enabled: llmEnabled,
  })

  // Profile synthesis ("生成画像") shares the extraction model — it is the same
  // "turn memory into structured entries" job at a different granularity, and
  // making the user configure a second model for it would be a second thing to
  // get wrong. Unlike extraction this is user-triggered, so it is not gated on
  // `llmExtractionEnabled` (which governs the automatic capture path): the
  // master switch still applies.
  const synthesizeProfile = buildLlmCompleter(ctx, {
    maxTokens: config.extractionMaxTokens ?? 2048,
    modelOverride: () => runtime.get().extractionModel,
    enabled: () => runtime.isEnabled(),
    label: 'profile synthesis',
  })

  // The panel's data operations (features 3-5) are served to the browser over
  // the Remote gateway; registration is reversible with the controller. The
  // gateway protocol is optional — if this deployment lacks it, features 3-5
  // are simply unavailable in the browser and the plugin degrades gracefully.
  try {
    new AtomMemoryController(ctx, bridge, runtime, () => state.error, synthesizeProfile)
  } catch (err) {
    ctx.logger(`[atom-memory] remote controller unavailable (${(err as Error)?.message ?? err})`)
  }

  // Scope context: the payload every scope-aware call carries, assembled from
  // the working directory the call site reports plus the deployment's explicit
  // tags. The filesystem facts behind it are cached per directory (see
  // `scope.ts`), so this is a map lookup on the hot path, not a walk.
  //
  // With `scopeEnabled: false` the builder resolves to `undefined` for every
  // call, and each consumer treats "no payload" as "send no `scope_context` at
  // all" — which is what keeps the RPC params identical to what they were
  // before scope awareness existed.
  const explicitTags = {
    org: config.scopeOrg,
    client: config.scopeClient,
    project: config.scopeProject,
    series: config.scopeSeries,
    phase: config.scopePhase,
  }
  const scopeContextAt = (cwd: string | undefined): ScopeContextPayload | undefined =>
    config.scopeEnabled === false ? undefined : scopeContextForCwd(cwd, { explicit: explicitTags })
  const scopeContextOf = (source?: SessionCwdSource): ScopeContextPayload | undefined =>
    scopeContextAt(sessionCwdOf(source))

  // Single ingestion point: LLM-first candidates -> persist_candidates, else
  // -> rule-based add (everything stays isolated in the Python process). `cwd`
  // is the session's working directory when the hook has the session at hand;
  // the rescue sweeps pass none and the write falls back to the plugin's own
  // directory.
  const capture = createCapture({
    isEnabled: () => runtime.isEnabled(),
    isReady: () => state.value,
    extract,
    call: (method, params) => bridge.call(method, params),
    scopeContextAt,
  })

  // System-prompt awareness + the session-start-frozen memory snapshot.
  //
  // Registered *before* the tools so the tool set can be handed the snapshot
  // handle: "what the model is currently being told" has to be the same cache
  // the prompt is served from, or the audit view is a re-render that can drift.
  //
  // `isEnabled` is the master switch: when off, the awareness section resolves
  // to empty (so no "You have persistent long-term memory…" text reaches the
  // system prompt) and snapshot injection stops entirely.
  //
  // The injected snapshot uses its own (smaller) budget and the compact render
  // depth: it is paid for on every request and is the view that must stay short
  // and priority-ordered, unlike the full list the `memory_summary_detail` tool
  // and the settings modal return.
  //
  // The budget is passed as a *getter*, not a value: the settings panel owns it
  // at runtime, and resolving it at each freeze is what lets a change apply to
  // sessions that have not frozen yet while leaving already-frozen sessions
  // (and their KV cache) untouched.
  const snapshot = registerMemoryContext({
    ctx,
    bridge,
    userScope: FALLBACK_SCOPE,
    resolveMaxTokens: () => clampInjectedSummaryTokens(runtime.get().injectedSummaryTokens),
    // Read per assembly: turning injection off must stop paying for it now, and
    // turning it back on must work without a restart.
    snapshotEnabled: () => runtime.get().contextInjectionEnabled,
    isEnabled: () => runtime.isEnabled(),
    scopeContext: scopeContextOf,
  })

  // Explicit memory tools. `extract` is shared with the capture path so the
  // model-driven `memory_add` uses the same LLM-first extraction (with the
  // rule path as fallback) instead of the rules-only bridge `add` call. The
  // write tools wait briefly for the store's verdict — `wait_ms` is a bounded
  // acknowledgement, not a synchronous pipeline.
  const disposers = registerMemoryTools({
    ctx,
    bridge,
    fallbackScope: FALLBACK_SCOPE,
    maxRecalledFacts: config.maxRecalledFacts ?? 10,
    summaryTokens: config.summaryTokens ?? 1500,
    extract,
    isEnabled: () => runtime.isEnabled(),
    resolveSummaryBudget: () =>
      clampInjectedSummaryTokens(runtime.get().injectedSummaryTokens),
    writeAckTimeoutMs: config.writeAckTimeoutMs ?? 0,
    snapshot,
    scopeContext: scopeContextOf,
  })
  for (const d of disposers) ctx.effect(() => d)

  // Durable capture hooks (per-message, periodic nudge).
  registerCapture(
    { ctx, capture, maxRecent: 20 },
    {
      // A getter: the panel's switch stops capture immediately rather than at
      // the next reload.
      captureEnabled: () => runtime.get().captureEnabled,
      nudgeEnabled: config.nudgeEnabled !== false,
      nudgeIntervalMs: (config.nudgeIntervalMinutes ?? 30) * 60_000,
    },
  ).forEach((d) => ctx.effect(() => d))

  // Settings namespace: the composition entry seeds the runtime; a settings
  // write replaces it live. This powers the memory master switch (feature 1)
  // and the LLM extraction model override (feature 2) without a restart.
  //
  // Registration runs on `inject(['settings'], …)` rather than a synchronous
  // `ctx.get('settings')`: `get` returns `undefined` while the settings provider's
  // fiber is not yet active, which silently skipped registration and left the
  // browser panel's switch/model grayed out. `inject` waits for the service,
  // mirroring the harness's own `settings.installSection` call sites.
  ctx.inject(['settings'], (settingsCtx: Context) => {
    const settings = settingsCtx.get('settings') as {
      installSection(
        owner: Context,
        ns: string,
        schema: unknown,
        entry: LiveRuntime,
        hooks: {
          setSource(current: () => LiveRuntime): void
          onChange(): void
          validate?(value: LiveRuntime): void
        },
      ): void
    } | undefined
    if (settings?.installSection === undefined) return
    let source: () => LiveRuntime = () => seedRuntime(config)
    settings.installSection(ctx, SETTINGS_NAMESPACE, LiveSettingsSchema, source(), {
      setSource: (current) => { source = current },
      onChange: () => { runtime.set(source()) },
    })
    ctx.logger(`[dsh-atom-memory] settings section "${SETTINGS_NAMESPACE}" registered`)
  })

  // One audit line per live change: when a session behaves differently from the
  // last one, "which switch moved" is the first question, and this answers it
  // without a debugger.
  runtime.subscribe(() => {
    const live = runtime.get()
    ctx.logger(
      `[atom-memory] live switches: enabled=${live.enabled} capture=${live.captureEnabled} `
      + `llm=${live.llmExtractionEnabled} inject=${live.contextInjectionEnabled} `
      + `budget=${live.injectedSummaryTokens}`,
    )
  })

  ctx.logger('[dsh-atom-memory] loaded')
}

/**
 * Schemastery schema for the live settings namespace. This mirrors only the
 * runtime-toggleable fields so a settings write maps 1:1 onto the Runtime.
 */
const LiveSettingsSchema: z<LiveRuntime> = z.object({
  enabled: z.boolean().default(true),
  captureEnabled: z.boolean().default(true),
  llmExtractionEnabled: z.boolean().default(true),
  contextInjectionEnabled: z.boolean().default(true),
  injectedSummaryTokens: z.number().default(DEFAULT_INJECTED_SUMMARY_TOKENS),
  extractionModel: z.object({
    provider: z.string().default(''),
    model: z.string().default(''),
    baseURL: z.string().default(''),
    protocol: z.string().default('openai'),
    apiKey: z.string().default(''),
  }).default({ provider: '', model: '', baseURL: '', protocol: 'openai', apiKey: '' }),
})
