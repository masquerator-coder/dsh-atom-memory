/**
 * Runtime-configuration holder for the dsh-atom-memory plugin.
 *
 * The plugin's initial behaviour is taken from the composition-entry config
 * (schemastery), but the settings panel can change a handful of "live" fields
 * at runtime through the `atom-memory` settings namespace. Rather than tear
 * down and rebuild the whole plugin (which would drop registration state), the
 * behaviours consult this holder at each call site and react to
 * `onChange` notifications.
 *
 * The holder carries only the live, user-toggleable fields; every other config
 * field is read once from the composition entry at apply time. This keeps the
 * mutable surface small and auditable.
 */

import { clampInjectedSummaryTokens } from './injection-budget.ts'

export interface ExtractionModelOverride {
  /** Manual provider id (e.g. `deepseek`) or a free-form label for a custom endpoint. */
  provider?: string
  /** Manual model name. Leave empty to follow the dsh default selection. */
  model?: string
  /** Custom OpenAI-compatible endpoint base URL (e.g. `https://api.example.com/v1`). */
  baseURL?: string
  /** Wire protocol the endpoint speaks. Only `openai` is currently supported. */
  protocol?: string
  /** API key for a custom endpoint (plaintext, per the settings-panel design). */
  apiKey?: string
}

/** The live fields the settings panel can toggle at runtime. */
export interface LiveRuntime {
  /** Whether per-message / rescue / nudge capture runs. */
  captureEnabled: boolean
  /** Whether the LLM-first extractor is used (rule fallback stays). */
  llmExtractionEnabled: boolean
  /** Whether the per-session frozen snapshot + awareness are injected. */
  contextInjectionEnabled: boolean
  /**
   * Estimated-token budget for the snapshot injected into the system prompt.
   *
   * Live rather than apply-time because it is the knob a user actually tunes:
   * the snapshot is paid for on every request, so its size is the first thing
   * to want smaller. A change applies to every session that has not yet frozen
   * its snapshot — sessions already frozen keep their byte-identical text, so
   * the prompt prefix (and the provider's KV cache) stays valid.
   */
  injectedSummaryTokens: number
  /**
   * Whether the out-of-band work-overview synthesis runs.
   *
   * Separate from {@link contextInjectionEnabled} because it is the only switch
   * in this plugin that spends model calls *without* the user asking for
   * anything. Turning it off leaves the injected snapshot intact — it falls back
   * to the deterministic overview — so it is a cost knob, not a feature kill.
   */
  overviewEnabled: boolean
  /** Manual LLM extraction model override; empty provider+model = follow dsh default. */
  extractionModel?: ExtractionModelOverride
}

/** Values used to seed the runtime before the settings document exists. */
export interface LiveRuntimeSeed extends Partial<Omit<LiveRuntime, 'extractionModel'>> {
  extractionModel?: ExtractionModelOverride
}

/** Resolve a seed into a complete runtime value (defaults applied, budget clamped). */
export function createRuntime(seed: LiveRuntimeSeed): LiveRuntime {
  return {
    captureEnabled: seed.captureEnabled ?? true,
    llmExtractionEnabled: seed.llmExtractionEnabled ?? true,
    contextInjectionEnabled: seed.contextInjectionEnabled ?? true,
    injectedSummaryTokens: clampInjectedSummaryTokens(seed.injectedSummaryTokens),
    overviewEnabled: seed.overviewEnabled ?? true,
    extractionModel: seed.extractionModel,
  }
}

/**
 * The live value reader every consumer depends on.
 *
 * Declared as an interface rather than a class because the settings layer owns
 * the references now: the plugin passes a `{ get() }` closure that re-reads the
 * volatile config, and nothing in the plugin mutates runtime state itself.
 * Keeping the narrow shape means the controller, tools and capture hooks are
 * unchanged by where the values come from.
 *
 * This replaced a mutable `Runtime` class that held a value and notified
 * subscribers. Once the settings document became the single source of truth
 * (read live through `dsh-settings`' volatile refs), the holder had no writer
 * and no subscribers left — keeping it would have been a second, silent place
 * the live values could have come from.
 */
export interface RuntimeReader {
  /** Snapshot of the current live values. */
  get(): LiveRuntime
}

/** Namespace id used for the plugin's settings section on the Host. */
export const SETTINGS_NAMESPACE = 'atom-memory'
