/**
 * Controller bridging the `atom-memory` settings namespace and the Host Remote
 * operations onto a reactive snapshot for the settings panel.
 *
 * The injected-summary budget and the extraction model ride the settings
 * document through the `atom-memory` **ConfigForm** the ui-settings provider owns
 * (`ctx.configForms.get(NAMESPACE)`); features 3-5 (profile, facts editing,
 * backup/restore) ride the Remote gateway (`ctx.remote.atomMemory`). The
 * controller owns no model-visible state — it only stages the panel's drafts and
 * forwards writes.
 *
 * HISTORY: the settings side used to be a `SettingsScope` obtained from the
 * deleted client `settingsScope` service. `ConfigForm` exposes the same
 * `getSnapshot` / `subscribe` / `set` trio for the same `{ status, value, ... }`
 * snapshot shape, so only the parameter type changed here.
 *
 * @module dsh-atom-memory/client/memory-settings-controller
 */

import type { ConfigForm, ConfigFormSnapshot } from '@deepseek-ai/dsh-client-ui-settings/client'
import { createSnapshotStore, type SnapshotStore } from '@deepseek-ai/dsh-client-store'
import {
  DEFAULT_INJECTED_SUMMARY_TOKENS,
  clampInjectedSummaryTokens,
} from '../injection-budget.ts'

/** The live settings section this panel edits (mirrors the Host side). */
export interface MemorySettingsSection {
  captureEnabled: boolean
  llmExtractionEnabled: boolean
  contextInjectionEnabled: boolean
  /** Estimated-token budget for the memory summary snapshot injected into the prompt. */
  injectedSummaryTokens: number
  /**
   * Whether the out-of-band work-overview synthesis runs.
   *
   * The only switch here that spends model calls on the user's behalf without
   * being asked to, so it is worth surfacing separately: turning it off leaves
   * injection intact (the snapshot falls back to the deterministic overview).
   */
  overviewEnabled: boolean
  extractionModel?: {
    provider?: string
    model?: string
    /** Custom OpenAI-compatible endpoint base URL (API 地址). */
    baseURL?: string
    /** Wire protocol (only `openai` supported). */
    protocol?: string
    /** API key for a custom endpoint (plaintext). */
    apiKey?: string
  }
}

/** The dynamic facts/profile/backup data the panel fetches via Remote. */
export interface MemoryData {
  facts: Array<{
    fact_id: string
    subject: string
    predicate: string
    object: string
    type?: string
    content?: string
  }>
  profile: Array<{ section: string; key: string; value: string; source?: string }>
  /** Row cap and current count for the profile table (`limit` 0 = uncapped). */
  profileCount?: number
  profileLimit?: number
  /** The rendered `summary` view (injected system-prompt memory), lazy-loaded. */
  summary?: string
}

/** What the panel renders. */
export interface MemorySettingsState {
  /** Whether the settings namespace is served and writable. */
  available: boolean
  loading: boolean
  section: MemorySettingsSection
  /** Dynamic data (facts + profile), fetched lazily. */
  data: MemoryData
  lastError?: string
}

/** The registration-side face the section's slot entry injects.
 *
 * Per `InjectFace`, the `hooks` compartment arrives as `use<Name>` hooks and
 * every other member passes through verbatim as props, so the actions live at
 * the top level (not nested under `actions`).
 */
export interface MemorySettingsFace {
  hooks: {
    /** Section snapshot bound by the renderer as useMemorySettings. */
    memorySettings: SnapshotStore<MemorySettingsState>
  }
  /**
   * Set the injected memory summary token budget.
   *
   * Clamped by the caller (`clampInjectedSummaryTokens`) before it gets here so the
   * panel and the Host agree on the bounds; the Host clamps again on the way in.
   *
   * `ConfigForm.set` resolves whether the Host ACCEPTED the write (`false` for a
   * refusal or a memory-mode skip), whereas the old `SettingsScope.set` resolved
   * `void`. The panel treats a settings write as fire-and-forget — the mirror
   * publishes the real state either way — so the boolean is surfaced rather than
   * dropped, and callers are free to ignore it.
   */
  setInjectedSummaryTokens: (tokens: number) => Promise<boolean>
  /** Turn the out-of-band work-overview synthesis on or off. */
  setOverviewEnabled: (enabled: boolean) => Promise<boolean>
  /**
   * Ask the store to regenerate the work overview now.
   *
   * Returns the refresher's outcome token (e.g. `refreshed`, `throttled`,
   * `no-change:up_to_date`) which the panel renders through `overviewRefreshDone`;
   * the token is deliberately not translated here, because the panel owns the
   * wording and the host owns the vocabulary.
   */
  refreshOverview: () => Promise<string>
  setExtractionModel: (provider: string, model: string) => Promise<boolean>
  /** Write the whole extraction-model override (provider/model/baseURL/protocol/apiKey). */
  setExtractionModelOverride: (override: NonNullable<MemorySettingsSection['extractionModel']>) => Promise<boolean>
  refreshData: () => Promise<void>
  saveFact: (fact: MemoryData['facts'][number]) => Promise<void>
  deleteFact: (factId: string) => Promise<void>
  /** Lazy-load the user's compact summary (the view injected into the prompt). */
  fetchSummary: () => Promise<string>
  upsertProfile: (section: string, key: string, value: string) => Promise<void>
  deleteProfile: (section: string, key: string) => Promise<void>
  /** Batch-save an Excel-style facts table (edit changed rows, delete marked rows) in one pass. */
  saveAllFacts: (rows: FactEditRow[]) => Promise<void>
  /** Batch-save the profile table (upsert changed rows, delete marked rows) in one pass. */
  saveAllProfile: (rows: ProfileEditRow[]) => Promise<void>
  /**
   * Ask the store + model for profile entries to propose, excluding rows the
   * profile already has. Returns the proposals for the user to approve; nothing
   * is written until {@link saveAllProfile} is called with them.
   */
  generateProfile: () => Promise<ProfileSuggestionResult>
  backup: () => Promise<Record<string, unknown>>
  restore: (payload: Record<string, unknown>) => Promise<{ facts_written: number; profile_written: number }>
}

/** One facts-table row edited by the Excel-style modal editor. */
export interface FactEditRow {
  fact_id: string
  subject: string
  predicate: string
  object: string
  content?: string
  type?: string
  /** Marked for soft-delete when saving. */
  deleted?: boolean
}

/** One profile-table row edited by the Excel-style modal editor. */
export interface ProfileEditRow {
  section: string
  key: string
  value: string
  /** Marked for deletion when saving. The section/key still identify the row. */
  deleted?: boolean
}

/** One generated profile entry awaiting approval. */
export interface ProfileSuggestion {
  section: string
  key: string
  value: string
}

/** The outcome of a "生成画像" run. */
export interface ProfileSuggestionResult {
  suggestions: ProfileSuggestion[]
  /** Rows already in the profile. */
  existing: number
  /** Row cap; 0 means uncapped. */
  limit: number
  /** The table is at its cap, so nothing can be accepted until a row is freed. */
  full: boolean
}

/** A minimal view of the wire result the client namespace methods resolve to.
 *
 * The dsh Client Remote `invoke` path returns `RemoteResult<unknown>` —
 * `{ ok: true, value: <method return> }` or `{ ok: false, error }` — NOT the
 * bare method return. Consumers unwrap `.value` (the harness's own idiom, e.g.
 * `settings-scope.ts` reads `response.value`). This interface models that shape.
 */
interface WireResult<T> {
  ok: boolean
  /** The Host method's JSON return when `ok` is true. */
  value: T
  error?: unknown
}

/** Minimal structural shape of the `atom-memory` Remote namespace. */
interface RemoteAtomMemory {
  listFacts(args: { user: string; offset?: number; limit?: number }): Promise<WireResult<{ facts: MemoryData['facts']; total: number }>>
  editFact(args: {
    user: string
    fact_id: string
    subject?: string
    predicate?: string
    object?: string
    content?: string
    type?: string
  }): Promise<WireResult<unknown>>
  deleteFact(args: { user: string; fact_id: string }): Promise<WireResult<unknown>>
  summary(args: { user: string; maxTokens?: number }): Promise<WireResult<string>>
  listProfile(args: { user: string }): Promise<WireResult<{ profile: MemoryData['profile']; count?: number; limit?: number }>>
  upsertProfile(args: { user: string; section: string; key: string; value: string }): Promise<WireResult<unknown>>
  deleteProfile(args: { user: string; section: string; key: string }): Promise<WireResult<unknown>>
  writeProfile(args: { user: string; rows: ProfileEditRow[] }): Promise<WireResult<unknown>>
  generateProfile(args: { user: string }): Promise<WireResult<ProfileSuggestionResult>>
  backup(args: { user: string }): Promise<WireResult<Record<string, unknown>>>
  restore(args: { user: string; payload: Record<string, unknown> }): Promise<WireResult<{ facts_written: number; profile_written: number }>>
  /** Regenerate the work overview; resolves to the refresher's outcome token. */
  refreshOverview(args: { user: string }): Promise<WireResult<string>>
}

/** Unwrap a `WireResult` to its `.value`, throwing on a failed call. */
function unwrap<T>(result: WireResult<T>): T {
  if (result == null || result.ok === false) {
    const message = result?.error != null
      ? String((result.error as { message?: unknown } | undefined)?.message ?? result.error)
      : 'Remote call failed'
    throw new Error(message)
  }
  return result.value
}

const USER = 'global'

export class MemorySettingsController {
  private readonly store = createSnapshotStore<MemorySettingsState>({
    available: false,
    loading: true,
    section: {
      captureEnabled: true,
      llmExtractionEnabled: true,
      contextInjectionEnabled: true,
      injectedSummaryTokens: DEFAULT_INJECTED_SUMMARY_TOKENS,
      overviewEnabled: true,
      extractionModel: undefined,
    },
    data: { facts: [], profile: [] },
  })
  private readonly unsubscribe: () => void

  constructor(
    private readonly scope: ConfigForm<MemorySettingsSection>,
    private readonly remote: unknown,
  ) {
    this.unsubscribe = scope.subscribe(() => this.publish())
    this.publish()
  }

  /** @returns the face the section's slot registration injects. */
  inject(): MemorySettingsFace {
    return {
      hooks: { memorySettings: this.store },
      setInjectedSummaryTokens: (tokens) =>
        this.scope.set('injectedSummaryTokens', clampInjectedSummaryTokens(tokens)),
      setOverviewEnabled: (enabled) => this.scope.set('overviewEnabled', enabled),
      refreshOverview: async () => {
        const outcome = unwrap(await this.r().refreshOverview({ user: USER }))
        // Regeneration changes what the summary modal renders, so drop the
        // cached text: otherwise the panel keeps showing the old overview and
        // reads as "the button did nothing".
        this.store.set({
          ...this.store.getSnapshot(),
          data: { ...this.store.getSnapshot().data, summary: undefined },
        })
        return outcome
      },
      setExtractionModel: (provider, model) =>
        this.scope.set('extractionModel', { provider, model }),
      setExtractionModelOverride: (override) =>
        this.scope.set('extractionModel', override),
      refreshData: () => this.refreshData(),
      saveFact: (fact) => this.saveFact(fact),
      deleteFact: (factId) => this.deleteFact(factId),
      fetchSummary: () => this.fetchSummary(),
      upsertProfile: (section, key, value) => this.upsertProfile(section, key, value),
      deleteProfile: (section, key) => this.deleteProfile(section, key),
      saveAllFacts: (rows) => this.saveAllFacts(rows),
      saveAllProfile: (rows) => this.saveAllProfile(rows),
      generateProfile: () => this.generateProfile(),
      backup: () => this.backup(),
      restore: (payload) => this.restore(payload),
    }
  }

  dispose(): void {
    this.unsubscribe()
  }

  private r(): RemoteAtomMemory {
    return this.remote as RemoteAtomMemory
  }

  private publish(): void {
    const snap = this.scope.getSnapshot()
    const value = snap.value
    this.store.set({
      available: snap.status === 'ready' || snap.status === 'loading',
      loading: snap.status === 'loading',
      section: value === undefined ? this.store.getSnapshot().section : defaulted(value),
      data: this.store.getSnapshot().data,
      lastError: this.store.getSnapshot().lastError,
    })
  }

  private async refreshData(): Promise<void> {
    try {
      const [factsR, profileR] = await Promise.all([
        this.r().listFacts({ user: USER, limit: 200 }),
        this.r().listProfile({ user: USER }),
      ])
      const facts = unwrap(factsR)
      const profile = unwrap(profileR)
      this.store.set({
        ...this.store.getSnapshot(),
        data: {
          facts: Array.isArray(facts.facts) ? facts.facts : [],
          profile: Array.isArray(profile.profile) ? profile.profile : [],
          profileCount: Number(profile.count ?? (Array.isArray(profile.profile) ? profile.profile.length : 0)),
          profileLimit: Number(profile.limit ?? 0),
        },
        lastError: undefined,
      })
    } catch (err) {
      this.store.set({
        ...this.store.getSnapshot(), lastError: (err as Error)?.message ?? String(err),
      })
    }
  }

  private async saveFact(fact: MemoryData['facts'][number]): Promise<void> {
    try {
      // `unwrap`: a `{ok:false}` refusal must not be read as a save. `type` is
      // carried here on purpose — this path edits the whole row the *list* view
      // rendered, so the value came from the store rather than from an editor
      // that never showed it.
      unwrap(await this.r().editFact({
        user: USER,
        fact_id: fact.fact_id,
        subject: fact.subject,
        predicate: fact.predicate,
        object: fact.object,
        content: fact.content,
        type: fact.type,
      }))
      await this.refreshData()
    } catch (err) {
      this.store.set({
        ...this.store.getSnapshot(), lastError: (err as Error)?.message ?? String(err),
      })
      throw err
    }
  }

  private async deleteFact(factId: string): Promise<void> {
    try {
      unwrap(await this.r().deleteFact({ user: USER, fact_id: factId }))
      await this.refreshData()
    } catch (err) {
      this.store.set({
        ...this.store.getSnapshot(), lastError: (err as Error)?.message ?? String(err),
      })
      throw err
    }
  }

  private async fetchSummary(): Promise<string> {
    try {
      const text = unwrap(await this.r().summary({ user: USER }))
      this.store.set({
        ...this.store.getSnapshot(),
        data: { ...this.store.getSnapshot().data, summary: text },
        lastError: undefined,
      })
      return text
    } catch (err) {
      this.store.set({
        ...this.store.getSnapshot(), lastError: (err as Error)?.message ?? String(err),
      })
      throw err
    }
  }

  private async upsertProfile(section: string, key: string, value: string): Promise<void> {
    try {
      await this.r().upsertProfile({ user: USER, section, key, value })
      await this.refreshData()
    } catch (err) {
      this.store.set({
        ...this.store.getSnapshot(), lastError: (err as Error)?.message ?? String(err),
      })
    }
  }

  private async deleteProfile(section: string, key: string): Promise<void> {
    try {
      await this.r().deleteProfile({ user: USER, section, key })
      await this.refreshData()
    } catch (err) {
      this.store.set({
        ...this.store.getSnapshot(), lastError: (err as Error)?.message ?? String(err),
      })
    }
  }

  private async saveAllFacts(rows: FactEditRow[]): Promise<void> {
    try {
      // Only the fields the user actually changed are sent. `edit_fact` reads
      // "present and not None" as "set this column", so passing a value the
      // editor never showed rewrites it: the modal binds subject/predicate/
      // object/content but NOT `type`, so every untouched row would have been
      // re-typed with the draft's empty string, which the store normalises to
      // `semantic`. That silently flattened SOP / lesson / decision_rule
      // knowledge — the types the archive pass protects and the ranking reads —
      // on a save where the user changed nothing at all.
      const before = this.store.getSnapshot().data.facts
      const originalById = new Map(before.map(f => [f.fact_id, f]))
      for (const row of rows) {
        if (row.deleted) {
          // `unwrap` for the same reason as the edit below: a `{ok:false}`
          // deletion would otherwise read as success and the panel would report
          // a save that silently left the fact in place.
          unwrap(await this.r().deleteFact({ user: USER, fact_id: row.fact_id }))
          continue
        }
        const original = originalById.get(row.fact_id)
        const patch: {
          user: string
          fact_id: string
          subject?: string
          predicate?: string
          object?: string
          content?: string
        } = { user: USER, fact_id: row.fact_id }
        if (original === undefined) {
          // No snapshot entry to diff against (the list was refreshed away, or
          // the caller supplied rows directly). Send the editable fields so the
          // edit still lands rather than being silently dropped — `type` is
          // still omitted, since the editor never showed it.
          patch.subject = row.subject
          patch.predicate = row.predicate
          patch.object = row.object
          patch.content = row.content ?? ''
        } else {
          if (row.subject !== original.subject) patch.subject = row.subject
          if (row.predicate !== original.predicate) patch.predicate = row.predicate
          if (row.object !== original.object) patch.object = row.object
          if ((row.content ?? '') !== (original.content ?? '')) patch.content = row.content ?? ''
        }
        // An untouched row produces an empty patch, and sending it would be a
        // write with no effect; skip it rather than spend a round trip.
        if (Object.keys(patch).length <= 2) continue
        unwrap(await this.r().editFact(patch))
      }
      await this.refreshData()
    } catch (err) {
      const message = (err as Error)?.message ?? String(err)
      this.store.set({ ...this.store.getSnapshot(), lastError: message })
      // The panel's save handler closes the modal only on success, so a
      // re-throw is what lets it keep the editor — and the edits not yet
      // applied — in front of the user. Mirrors `saveAllProfile`.
      throw err
    }
  }

  private async saveAllProfile(rows: ProfileEditRow[]): Promise<void> {
    try {
      // One batch call rather than a row-by-row loop: the store enforces the
      // row cap on the whole batch, so a save that would exceed it is refused
      // as a unit and the table keeps its previous state. A per-row loop could
      // apply half the edits and then fail on the row that crossed the cap.
      // `unwrap` matters here: without it a `{ok:false}` refusal would be read
      // as success and the panel would report a save that never happened.
      unwrap(await this.r().writeProfile({ user: USER, rows }))
      await this.refreshData()
    } catch (err) {
      const message = (err as Error)?.message ?? String(err)
      this.store.set({ ...this.store.getSnapshot(), lastError: message })
      // The panel's save handler closes the modal unconditionally, so a refusal
      // would otherwise vanish. Re-throw to let it keep the editor open.
      throw err
    }
  }

  private async generateProfile(): Promise<ProfileSuggestionResult> {
    const result = unwrap(await this.r().generateProfile({ user: USER }))
    const suggestions = Array.isArray(result?.suggestions) ? result.suggestions : []
    return {
      suggestions,
      existing: Number(result?.existing ?? 0),
      limit: Number(result?.limit ?? 0),
      full: result?.full === true,
    }
  }

  private async backup(): Promise<Record<string, unknown>> {
    return unwrap(await this.r().backup({ user: USER }))
  }

  private async restore(payload: Record<string, unknown>): Promise<{ facts_written: number; profile_written: number }> {
    const result = unwrap(await this.r().restore({ user: USER, payload }))
    await this.refreshData()
    return {
      facts_written: Number(result.facts_written ?? 0),
      profile_written: Number(result.profile_written ?? 0),
    }
  }
}

/** Fill defaults onto a (possibly partial / identical) section value. */
function defaulted(value: MemorySettingsSection): MemorySettingsSection {
  return {
    captureEnabled: value.captureEnabled ?? true,
    llmExtractionEnabled: value.llmExtractionEnabled ?? true,
    contextInjectionEnabled: value.contextInjectionEnabled ?? true,
    injectedSummaryTokens: clampInjectedSummaryTokens(value.injectedSummaryTokens),
    overviewEnabled: value.overviewEnabled ?? true,
    extractionModel: value.extractionModel,
  }
}
