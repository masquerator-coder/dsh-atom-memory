/**
 * Host Remote controller exposing the dsh-atom-memory operations to the
 * browser settings panel.
 *
 * The panel edits memory / user profile and drives backup & restore in the
 * Python store; those are data operations, not scalar namespaces, so they are
 * carried over the dsh Remote gateway rather than the settings document. This
 * controller is a thin, structured bridge to the Python child process — it
 * never reads or synthesises model-visible content itself, satisfying the
 * "model-visible <=> logged" invariant by leaving all memory logic in Python.
 *
 * @module dsh-atom-memory/controller
 */
import { Context } from '@deepseek-ai/cordis'
import { Remote, TypertRemoteService } from '@deepseek-ai/dsh-typert-protocol'
import type { PythonBridge } from './bridge.ts'
import { clampInjectedSummaryTokens } from './injection-budget.ts'
import type { LiveRuntime, RuntimeReader } from './runtime.ts'
import type { LlmCompleter } from './llm-extractor.ts'
import type { ChangeRow } from './tools.ts'
import {
  synthesizeProfileSuggestions,
  type ProfileCandidate,
  type ProfileSuggestion as SynthesizedSuggestion,
} from './profile-synthesis.ts'

/** One active fact as the panel edits it. */
export interface FactEditInput {
  fact_id: string
  subject?: string
  predicate?: string
  object?: string
  content?: string
  type?: string
}

/** One profile row as the panel edits it. */
export interface ProfileEditInput {
  section: string
  key?: string
  value: string
}

/**
 * One profile-table row edit from the panel's batch save.
 *
 * `deleted` marks a row the user removed. The row still has to name its
 * `section`/`key`, because that is what identifies which row to delete.
 */
export interface ProfileRowEdit {
  section: string
  key: string
  value?: string
  deleted?: boolean
}

/** One suggested profile entry awaiting the user's approval. */
export interface ProfileSuggestion {
  section: string
  key: string
  value: string
}

/**
 * One row of the user's topic vocabulary (`domain` table).
 *
 * Mirror of the Python `DomainRow.to_dict()` surface. `name` is the canonical
 * (lowercase, path-like) form and `display_name` the human one; `path` is the
 * materialised hierarchy label, which is what the panel shows because it is the
 * only field that makes a nested topic readable at a glance.
 */
export interface DomainRow {
  domain_id: number
  name: string
  display_name: string
  parent_id?: number | null
  path?: string
  status?: string
  system_seeded?: boolean
}

declare module '@deepseek-ai/cordis' {
  interface Context {
    /** Host owner of the `atom-memory` Remote namespace for the memory panel. */
    atomMemoryController: AtomMemoryController
  }
}

/**
 * Host service backing `ctx.remote.atomMemory`. Every method delegates to the
 * Python bridge and returns a JSON-serializable business value (backup payloads
 * are plain JSON). Arguments are validated minimally here and fully by the
 * Python side.
 */
export class AtomMemoryController extends TypertRemoteService {
  constructor(
    ctx: Context,
    private readonly bridge: PythonBridge,
    private readonly runtime: RuntimeReader,
    /**
     * Why the bridge is not running, when the plugin knows (preflight failure,
     * spawn error). Reported verbatim to the panel: "memory bridge is not
     * running" without the reason is the least actionable message a diagnostic
     * surface can produce.
     */
    private readonly startupError: () => string | undefined = () => undefined,
    /**
     * The completion used for profile synthesis, resolved from the dsh LLM.
     *
     * `undefined` when no model is available: profile generation is then an
     * honest "no model configured" error rather than an empty suggestion list
     * that would read as "your memory has nothing worth keeping".
     */
    private readonly complete: LlmCompleter | undefined = undefined,
    /**
     * Run one overview refresh on demand (the panel's "regenerate" button).
     * Absent when overview maintenance is off, in which case the panel is told
     * so instead of appearing to work.
     */
    private readonly refreshOverviewFn: (() => Promise<string>) | undefined = undefined,
  ) {
    super(ctx, 'atomMemoryController', { namespace: 'atomMemory' })
  }

  /** Whether the bridge is alive and therefore able to serve a call. */
  private assertReady(): void {
    if (!this.bridge.alive) {
      const reason = this.startupError()
      throw new Error(
        reason !== undefined
          ? `memory bridge is not running: ${reason}`
          : 'memory bridge is not running',
      )
    }
  }

  /**
   * Diagnostics for the panel: is the store actually usable?
   *
   * Deliberately does not call {@link assertReady}: this is the call the panel
   * makes *because* something is wrong, so it has to answer while the bridge is
   * down instead of throwing the same generic error.
   */
  @Remote
  async health(): Promise<Record<string, unknown>> {
    const startupError = this.startupError()
    const payload = await this.bridge.healthDetail()
    return {
      bridgeAlive: this.bridge.alive,
      startupError: startupError ?? null,
      startup: payload ?? null,
    }
  }

  /** Paginate the user's active facts. */
  @Remote
  async listFacts(args: { user: string; offset?: number; limit?: number; includeRetracted?: boolean }):
  Promise<Record<string, unknown>> {
    this.assertReady()
    return this.bridge.call('list_facts', {
      user_id: args.user,
      offset: args.offset ?? 0,
      limit: args.limit ?? 50,
      include_retracted: args.includeRetracted ?? false,
    }) as Promise<Record<string, unknown>>
  }

  /** Directly edit one active fact's SPO / type / content. */
  @Remote
  async editFact(args: { user: string } & FactEditInput): Promise<Record<string, unknown>> {
    this.assertReady()
    if (!args.fact_id) throw new Error('editFact requires fact_id')
    return this.bridge.call('edit_fact', {
      user_id: args.user,
      fact_id: args.fact_id,
      subject: args.subject,
      predicate: args.predicate,
      object: args.object,
      content: args.content,
      type: args.type,
    }) as Promise<Record<string, unknown>>
  }

  /** Soft-retract (forget) one active fact. */
  @Remote
  async deleteFact(args: { user: string; fact_id: string }): Promise<Record<string, unknown>> {
    this.assertReady()
    if (!args.fact_id) throw new Error('deleteFact requires fact_id')
    return this.bridge.call('forget', {
      user_id: args.user,
      fact_id: args.fact_id,
    }) as Promise<Record<string, unknown>>
  }

  /**
   * Render the user's `summary` exactly as the host injects it.
   *
   * The panel's "view memory" modal must show the *same text the model sees*,
   * so this asks for the compact depth (`detail: false`) the session system
   * prompt is frozen from: grouped by memory type, priority-ordered, no
   * `fact_id`. The full list with `fact_id`s stays available through
   * `memory_summary` with `detail: true`, whose whole purpose is locating a fact
   * to edit.
   */
  @Remote
  async summary(args: { user: string; maxTokens?: number }): Promise<string> {
    this.assertReady()
    const result = await this.bridge.call<{ text?: string }>('summary', {
      user_id: args.user,
      // Same budget the session prompt freezes at, so the modal shows the text
      // the model would actually receive. A session that already froze keeps its
      // frozen copy — `memory_snapshot` is the tool-side view of that.
      max_tokens: args.maxTokens ?? clampInjectedSummaryTokens(this.runtime.get().injectedSummaryTokens),
      detail: false,
      // The head is part of what the model sees, so the modal has to render it
      // too — otherwise the panel would show a text no session ever received.
      overview: true,
    })
    // `AtomMem.summary` returns the markdown string directly; tolerate a
    // wrapped shape in case the Python side ever changes the contract.
    return typeof result === 'string' ? result : (result?.text ?? '')
  }

  /**
   * The memory changelog: what the store did lately, newest first.
   *
   * The panel's answer to "did anything change?", and the same source the
   * overview refresher gates on — so a user seeing "no changes" here and no
   * overview refresh is seeing one consistent fact, not two implementations
   * agreeing by luck.
   */
  @Remote
  async changes(args: { user: string; sinceMs?: number; limit?: number }): Promise<{
    changes: ChangeRow[]
    level?: string
  }> {
    this.assertReady()
    return this.bridge.call('changes', {
      user_id: args.user,
      ...(args.sinceMs === undefined ? {} : { since_ms: args.sinceMs }),
      ...(args.limit === undefined ? {} : { limit: args.limit }),
    })
  }

  /**
   * The overview cache's state, including whether a refresh is warranted.
   *
   * Read-only on purpose: the panel must be able to answer "why is the overview
   * stale / why did nothing regenerate" without triggering the very generation
   * it is asking about.
   */
  @Remote
  async overviewStatus(args: { user: string }): Promise<Record<string, unknown>> {
    this.assertReady()
    return this.bridge.call('overview_status', { user_id: args.user })
  }

  /**
   * Regenerate the overview now.
   *
   * User-triggered, so it bypasses the debounce and the minimum gap but still
   * consults the changelog gate — an explicit refresh of an unchanged store is
   * still a wasted completion, and the panel reports the outcome either way.
   */
  @Remote
  async refreshOverview(args: { user: string }): Promise<string> {
    this.assertReady()
    if (this.refreshOverviewFn === undefined) {
      return '（本部署未启用总览后台生成）'
    }
    return this.refreshOverviewFn()
  }

  /**
   * Restore an archived fact to the active set.
   *
   * The archive tier is how capacity control stays non-destructive: nothing is
   * deleted when the store is over its cap, so there has to be a way back.
   */
  @Remote
  async unarchive(args: { user: string; fact_id: string }): Promise<Record<string, unknown>> {
    this.assertReady()
    if (!args.fact_id) throw new Error('unarchive requires fact_id')
    return this.bridge.call('unarchive', {
      user_id: args.user,
      fact_id: args.fact_id,
    }) as Promise<Record<string, unknown>>
  }

  /** List the user's profile rows. */
  @Remote
  async listProfile(args: { user: string }): Promise<Record<string, unknown>> {
    this.assertReady()
    return this.bridge.call('list_profile', { user_id: args.user }) as Promise<Record<string, unknown>>
  }

  /**
   * The user's registered topic vocabulary (the `memory_domains`词表), for the
   * settings panel's memory summary.
   *
   * `domain_list` answers with a bare list, so it is wrapped in an object here:
   * every other panel-facing method returns a named-key envelope, and a bare
   * array at the wire boundary is the shape that tends to get silently coerced
   * (or dropped) by an intermediate layer. The panel reads `domains`.
   *
   * Read-only: it never registers a topic. Registering happens on the write path
   * (`memory_domains` create / an extractor's proposal), never as a side effect
   * of drawing a count.
   */
  @Remote
  async listDomains(args: { user: string }): Promise<{ domains: DomainRow[] }> {
    this.assertReady()
    const domains = await this.bridge.call('domain_list', { user_id: args.user })
    return { domains: Array.isArray(domains) ? domains as DomainRow[] : [] }
  }

  /** Add or update one profile row (a user edit from the panel). */
  @Remote
  async upsertProfile(args: { user: string } & ProfileEditInput): Promise<Record<string, unknown>> {
    this.assertReady()
    if (!args.section || !args.key) throw new Error('upsertProfile requires section and key')
    return this.bridge.call('upsert_profile', {
      user_id: args.user,
      section: args.section,
      key: args.key,
      value: args.value,
    }) as Promise<Record<string, unknown>>
  }

  /** Delete one profile row. */
  @Remote
  async deleteProfile(args: { user: string; section: string; key: string }): Promise<Record<string, unknown>> {
    this.assertReady()
    return this.bridge.call('delete_profile', {
      user_id: args.user,
      section: args.section,
      key: args.key,
    }) as Promise<Record<string, unknown>>
  }

  /** Apply the panel's batch profile edits (upserts + deletions) in one pass. */
  @Remote
  async writeProfile(args: { user: string; rows: ProfileRowEdit[] }): Promise<Record<string, unknown>> {
    this.assertReady()
    return this.bridge.call('write_profile', {
      user_id: args.user,
      rows: args.rows ?? [],
    }) as Promise<Record<string, unknown>>
  }

  /**
   * Propose profile entries for the user to approve.
   *
   * The flow spans both halves on purpose: Python owns which slots are *filable*
   * (the deterministic aggregation excludes facts that are already represented),
   * and the model — which lives here on the dsh side — owns which of those are
   * worth keeping. Nothing is written: the result is a proposal list, and the
   * rows land only when the user accepts them through {@link writeProfile}.
   */
  @Remote
  async generateProfile(args: { user: string }): Promise<Record<string, unknown>> {
    this.assertReady()
    if (this.complete === undefined) {
      throw new Error('未配置可用模型：请在插件设置里指定抽取模型，或让 dsh 有默认模型')
    }
    const raw = await this.bridge.call('profile_candidates', { user_id: args.user }) as {
      candidates?: ProfileCandidate[]
      existing_keys?: Array<[string, string]>
      existing?: number
      limit?: number
      remaining?: number
    }
    const candidates = Array.isArray(raw?.candidates) ? raw.candidates : []
    const limit = Number(raw?.limit ?? 0)
    const remaining = Number(raw?.remaining ?? 0)

    // Nothing filable, or no free slot: say which, rather than reporting an
    // empty suggestion list that reads as "your memory has nothing to offer".
    if (limit > 0 && remaining <= 0) {
      return { suggestions: [], existing: Number(raw?.existing ?? 0), limit, full: true }
    }
    if (candidates.length === 0) {
      return { suggestions: [], existing: Number(raw?.existing ?? 0), limit, full: false }
    }

    // The profile's own pairs come back with the candidates, so filtering needs
    // no second round trip (and cannot race an edit made in between).
    const existingPairs = new Set(
      (Array.isArray(raw?.existing_keys) ? raw.existing_keys : [])
        .map(pair => `${String(pair?.[0] ?? '')}\u0000${String(pair?.[1] ?? '')}`),
    )

    const suggestions: SynthesizedSuggestion[] = await synthesizeProfileSuggestions(
      this.complete,
      candidates,
      { existing: existingPairs, remaining, limit },
    )
    return {
      suggestions,
      existing: Number(raw?.existing ?? 0),
      limit,
      full: false,
    }
  }

  /** Export the user's memory as a JSON snapshot (for download). */
  @Remote
  async backup(args: { user: string }): Promise<Record<string, unknown>> {
    this.assertReady()
    return this.bridge.call('backup', { user_id: args.user }) as Promise<Record<string, unknown>>
  }

  /** Import a JSON snapshot, replacing the user's memory. */
  @Remote
  async restore(args: { user: string; payload: Record<string, unknown> }): Promise<Record<string, unknown>> {
    this.assertReady()
    if (!args.payload || typeof args.payload !== 'object') throw new Error('restore requires a backup payload')
    return this.bridge.call('restore', {
      user_id: args.user,
      payload: args.payload,
    }) as Promise<Record<string, unknown>>
  }

  /** Read the current live runtime (capture / injection switches, model override). */
  @Remote
  async getRuntime(): Promise<LiveRuntime> {
    return this.runtime.get()
  }
}
