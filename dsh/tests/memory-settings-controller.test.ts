/**
 * Unit tests for the browser settings controller (`src/client/`).
 *
 * The controller is plain TS (no DOM), so it runs in the node vitest env. It
 * bridges the `atom-memory` settings ConfigForm (features 1 & 2) and the Remote
 * operations (features 3-5) onto a snapshot store for the settings panel.
 *
 * The stub below is a `ConfigForm`, the service DSH 0.1.7-alpha.1 replaced the
 * deleted client `settingsScope` with; only the method names changed.
 */
import { describe, expect, it, vi } from 'vitest'
import type { ConfigForm, ConfigFormSnapshot } from '@deepseek-ai/dsh-client-ui-settings/client'
import {
  MemorySettingsController,
  FACTS_PAGE_SIZE_DEFAULT,
  FACTS_PAGE_SIZES,
  type MemorySettingsSection,
} from '../src/client/memory-settings-controller.ts'
import {
  DEFAULT_INJECTED_SUMMARY_TOKENS,
  MAX_INJECTED_SUMMARY_TOKENS,
  MIN_INJECTED_SUMMARY_TOKENS,
} from '../src/injection-budget.ts'

function snapshot(over: Partial<ConfigFormSnapshot<MemorySettingsSection>>): ConfigFormSnapshot<MemorySettingsSection> {
  return {
    status: 'ready',
    value: {
      captureEnabled: true,
      llmExtractionEnabled: true,
      contextInjectionEnabled: true,
      injectedSummaryTokens: DEFAULT_INJECTED_SUMMARY_TOKENS,
      overviewEnabled: true,
      extractionModel: undefined,
      ...(over.value as Partial<MemorySettingsSection> | undefined),
    },
    base: undefined,
    user: undefined,
    revision: 1,
    writable: true,
    mode: 'host',
    ...over,
  }
}

function fakeScope(initial: ConfigFormSnapshot<MemorySettingsSection>) {
  const set = vi.fn(async (_field: string, _value: unknown) => true)
  const unset = vi.fn(async (_field: string) => true)
  const scope: ConfigForm<MemorySettingsSection> = {
    getSnapshot: () => initial,
    subscribe: () => () => {},
    mutate: vi.fn(async () => true),
    set,
    unset,
  }
  return { scope, set, unset }
}

function fakeRemote() {
  const backup = vi.fn(async () => ({ ok: true, value: { version: 1, facts: [], profile: [] } }))
  const restore = vi.fn(async () => ({ ok: true, value: { facts_written: 2, profile_written: 1 } }))
  const listFacts = vi.fn(async (): Promise<{ ok: boolean; value: { facts: Array<{ fact_id: string; subject: string; predicate: string; object: string }>; total: number } }> => ({ ok: true, value: { facts: [], total: 0 } }))
  const listProfile = vi.fn(async () => ({ ok: true, value: { profile: [], count: 0, limit: 50 } }))
  const deleteFact = vi.fn(async () => ({ ok: true, value: {} }))
  // Typed `unknown` so a test can make it fail or answer a malformed payload.
  const listDomains = vi.fn(async (): Promise<unknown> => ({ ok: true, value: { domains: [] } }))
  // Typed as `unknown` result so a test can make one call fail (or return a
  // malformed payload) without fighting the inferred success shape.
  const writeProfile = vi.fn(async (): Promise<unknown> => ({ ok: true, value: { written: 0, deleted: 0 } }))
  const generateProfile = vi.fn(async (): Promise<unknown> => ({
    ok: true,
    value: { suggestions: [], existing: 0, limit: 50, full: false },
  }))
  const summary = vi.fn(async () => ({ ok: true, value: '# 记忆摘要 (Summary) — global\n决策规则\n- 一条规则' }))
  const refreshOverview = vi.fn(async (): Promise<unknown> => ({ ok: true, value: 'refreshed' }))
  const remote: Record<string, unknown> = { listFacts, editFact: vi.fn(async () => ({ ok: true, value: {} })), deleteFact, listDomains, summary, listProfile, upsertProfile: vi.fn(async () => ({ ok: true, value: {} })), deleteProfile: vi.fn(async () => ({ ok: true, value: {} })), writeProfile, generateProfile, backup, restore, refreshOverview }
  return { remote, backup, restore, listFacts, listProfile, listDomains, deleteFact, summary, writeProfile, generateProfile, refreshOverview }
}

describe('MemorySettingsController', () => {
  it('publishes the initial settings snapshot into the store', () => {
    const { scope } = fakeScope(snapshot({ value: { overviewEnabled: false } as MemorySettingsSection }))
    const controller = new MemorySettingsController(scope as unknown as ConfigForm<MemorySettingsSection>, [])
    const face = controller.inject()
    const state = face.hooks.memorySettings.getSnapshot()
    expect(state.available).toBe(true)
    expect(state.section.overviewEnabled).toBe(false)
    expect(state.loading).toBe(false)
  })

  it('exposes top-level face actions alongside the memorySettings hook', () => {
    const { scope } = fakeScope(snapshot({}))
    const controller = new MemorySettingsController(scope as unknown as ConfigForm<MemorySettingsSection>, [])
    const face = controller.inject()
    // InjectFace maps hooks -> useX, other members pass through.
    expect(typeof face.setInjectedSummaryTokens).toBe('function')
    expect(typeof face.setExtractionModel).toBe('function')
    expect(typeof face.backup).toBe('function')
    expect(typeof face.restore).toBe('function')
    expect(typeof face.hooks.memorySettings.getSnapshot).toBe('function')
  })

  it('routes setOverviewEnabled through the settings scope', async () => {
    const { scope, set } = fakeScope(snapshot({}))
    const controller = new MemorySettingsController(scope as unknown as ConfigForm<MemorySettingsSection>, [])
    await controller.inject().setOverviewEnabled(false)
    expect(set).toHaveBeenCalledWith('overviewEnabled', false)
  })

  it('routes the extraction model override through the settings scope', async () => {
    const { scope, set } = fakeScope(snapshot({}))
    const controller = new MemorySettingsController(scope as unknown as ConfigForm<MemorySettingsSection>, [])
    await controller.inject().setExtractionModel('deepseek', 'deepseek-chat')
    expect(set).toHaveBeenCalledWith('extractionModel', { provider: 'deepseek', model: 'deepseek-chat' })
  })

  it('routes the full extraction-model override (provider/model/baseURL/protocol/apiKey)', async () => {
    const { scope, set } = fakeScope(snapshot({}))
    const controller = new MemorySettingsController(scope as unknown as ConfigForm<MemorySettingsSection>, [])
    await controller.inject().setExtractionModelOverride({
      provider: 'custom', model: 'gpt-4o-mini',
      baseURL: 'https://api.example.com/v1', protocol: 'openai', apiKey: 'sk-test',
    })
    expect(set).toHaveBeenCalledWith('extractionModel', {
      provider: 'custom', model: 'gpt-4o-mini',
      baseURL: 'https://api.example.com/v1', protocol: 'openai', apiKey: 'sk-test',
    })
  })

  it('routes the injection budget through the settings scope, clamped', async () => {
    const { scope, set } = fakeScope(snapshot({}))
    const controller = new MemorySettingsController(scope as unknown as ConfigForm<MemorySettingsSection>, [])
    const face = controller.inject()

    await face.setInjectedSummaryTokens(1200)
    expect(set).toHaveBeenCalledWith('injectedSummaryTokens', 1200)

    // The panel may hand over anything a text field produced; the controller is
    // the last line of defence before the Host (which clamps again).
    await face.setInjectedSummaryTokens(Number.NaN)
    expect(set).toHaveBeenCalledWith('injectedSummaryTokens', DEFAULT_INJECTED_SUMMARY_TOKENS)
    await face.setInjectedSummaryTokens(-5)
    expect(set).toHaveBeenCalledWith('injectedSummaryTokens', MIN_INJECTED_SUMMARY_TOKENS)
    await face.setInjectedSummaryTokens(9_999_999)
    expect(set).toHaveBeenCalledWith('injectedSummaryTokens', MAX_INJECTED_SUMMARY_TOKENS)
  })

  it('defaults a missing injection budget instead of exposing undefined', () => {
    const { scope } = fakeScope(snapshot({ value: { injectedSummaryTokens: undefined } as never }))
    const controller = new MemorySettingsController(scope as unknown as ConfigForm<MemorySettingsSection>, [])
    expect(controller.inject().hooks.memorySettings.getSnapshot().section.injectedSummaryTokens)
      .toBe(DEFAULT_INJECTED_SUMMARY_TOKENS)
  })

  it('calls the Remote namespace for backup and restore', async () => {
    const { scope } = fakeScope(snapshot({}))
    const { remote, backup, restore } = fakeRemote()
    const controller = new MemorySettingsController(scope as unknown as ConfigForm<MemorySettingsSection>, remote)
    const face = controller.inject()
    const exported = await face.backup()
    expect(backup).toHaveBeenCalledWith({ user: 'global' })
    expect(exported.version).toBe(1)
    const result = await face.restore({ version: 1, facts: [], profile: [] })
    expect(restore).toHaveBeenCalledWith({ user: 'global', payload: { version: 1, facts: [], profile: [] } })
    expect(result.facts_written).toBe(2)
    expect(result.profile_written).toBe(1)
  })

  it('refreshes dynamic data through the Remote list calls', async () => {
    const { scope } = fakeScope(snapshot({}))
    const { remote, listFacts, listProfile } = fakeRemote()
    listFacts.mockResolvedValue({
      ok: true,
      value: { facts: [{ fact_id: 'f1', subject: 's', predicate: 'p', object: 'o' }], total: 1 },
    })
    const controller = new MemorySettingsController(scope as unknown as ConfigForm<MemorySettingsSection>, remote)
    await controller.inject().refreshData()
    // The initial load is the first page, not the whole table: the panel pages
    // through `list_facts` rather than pulling everything and slicing it.
    expect(listFacts).toHaveBeenCalledWith({ user: 'global', offset: 0, limit: FACTS_PAGE_SIZE_DEFAULT })
    expect(listProfile).toHaveBeenCalledWith({ user: 'global' })
    const snap = controller.inject().hooks.memorySettings.getSnapshot()
    // Regression: the wire shape is `{ok, value}` — data must hold the raw
    // arrays (never undefined), otherwise `state.data.profile.length` throws
    // and the settings section blanks.
    expect(Array.isArray(snap.data.facts)).toBe(true)
    expect(Array.isArray(snap.data.profile)).toBe(true)
    expect(snap.data.facts[0]?.fact_id).toBe('f1')
  })

  it('carries the store total separately from the page so the count is not the page size', async () => {
    const { scope } = fakeScope(snapshot({}))
    const { remote, listFacts } = fakeRemote()
    listFacts.mockResolvedValue({
      ok: true,
      value: {
        facts: [{ fact_id: 'f1', subject: 's', predicate: 'p', object: 'o' }],
        // 137 facts in the store, 1 on this page: `facts.length` must never be
        // mistaken for the memory count.
        total: 137,
      },
    } as never)
    const controller = new MemorySettingsController(scope as unknown as ConfigForm<MemorySettingsSection>, remote)
    await controller.inject().refreshData()
    const snap = controller.inject().hooks.memorySettings.getSnapshot()
    expect(snap.data.facts).toHaveLength(1)
    expect(snap.data.factsTotal).toBe(137)
  })

  it('falls back to the page length when the store omits total', async () => {
    const { scope } = fakeScope(snapshot({}))
    const { remote, listFacts } = fakeRemote()
    listFacts.mockResolvedValue({
      ok: true,
      value: { facts: [{ fact_id: 'f1', subject: 's', predicate: 'p', object: 'o' }] },
    } as never)
    const controller = new MemorySettingsController(scope as unknown as ConfigForm<MemorySettingsSection>, remote)
    await controller.inject().refreshData()
    // A count rendered as `NaN` would read as "0 memories", so the payload
    // without a `total` degrades to the page length instead.
    expect(controller.inject().hooks.memorySettings.getSnapshot().data.factsTotal).toBe(1)
  })

  it('fetches a requested page with the exact offset and limit', async () => {
    const { scope } = fakeScope(snapshot({}))
    const { remote, listFacts } = fakeRemote()
    listFacts.mockResolvedValue({
      ok: true,
      value: {
        facts: [{ fact_id: 'f101', subject: 's', predicate: 'p', object: 'o' }],
        total: 240,
      },
    } as never)
    const controller = new MemorySettingsController(scope as unknown as ConfigForm<MemorySettingsSection>, remote)
    const face = controller.inject()
    // Page 3 of 100-row pages starts at row 200.
    await face.fetchFactsPage(200, 100)
    expect(listFacts).toHaveBeenLastCalledWith({ user: 'global', offset: 200, limit: 100 })
    const snap = face.hooks.memorySettings.getSnapshot()
    // The page REPLACES the list — the table renders one page at a time.
    expect(snap.data.facts.map(f => f.fact_id)).toEqual(['f101'])
    expect(snap.data.factsTotal).toBe(240)
  })

  it('offers only page sizes the store can actually serve', () => {
    // `list_facts` clamps `limit` to 200, so a larger option would render a
    // control that silently does not do what it says.
    expect(FACTS_PAGE_SIZES.every(size => size >= 1 && size <= 200)).toBe(true)
    expect(FACTS_PAGE_SIZES).toContain(FACTS_PAGE_SIZE_DEFAULT)
  })

  it('reads the registered domain vocabulary alongside the facts', async () => {
    const { scope } = fakeScope(snapshot({}))
    const { remote, listDomains } = fakeRemote()
    listDomains.mockResolvedValue({
      ok: true,
      value: {
        domains: [
          { domain_id: 1, name: 'programming', display_name: '编程', path: '/programming' },
          { domain_id: 2, name: 'teaching', display_name: '教学', path: '/teaching' },
        ],
      },
    } as never)
    const controller = new MemorySettingsController(scope as unknown as ConfigForm<MemorySettingsSection>, remote)
    await controller.inject().refreshData()
    expect(listDomains).toHaveBeenCalledWith({ user: 'global' })
    const snap = controller.inject().hooks.memorySettings.getSnapshot()
    expect(snap.data.domains?.map(d => d.name)).toEqual(['programming', 'teaching'])
  })

  it('keeps the facts list when the domain vocabulary cannot be read', async () => {
    const { scope } = fakeScope(snapshot({}))
    const { remote, listFacts, listDomains } = fakeRemote()
    listFacts.mockResolvedValue({
      ok: true,
      value: { facts: [{ fact_id: 'f1', subject: 's', predicate: 'p', object: 'o' }], total: 9 },
    } as never)
    listDomains.mockRejectedValue(new Error('domain surface unavailable'))
    const controller = new MemorySettingsController(scope as unknown as ConfigForm<MemorySettingsSection>, remote)
    // The vocabulary is optional context for a badge: losing it must not take
    // the working panel down with it.
    await expect(controller.inject().refreshData()).resolves.toBeUndefined()
    const snap = controller.inject().hooks.memorySettings.getSnapshot()
    expect(snap.data.facts.map(f => f.fact_id)).toEqual(['f1'])
    expect(snap.data.factsTotal).toBe(9)
    expect(snap.data.domains).toEqual([])
    // A supplementary failure is not surfaced as a panel-wide error either.
    expect(snap.lastError).toBeUndefined()
  })

  it('normalizes a malformed domain payload to an empty list', async () => {
    const { scope } = fakeScope(snapshot({}))
    const { remote, listDomains } = fakeRemote()
    listDomains.mockResolvedValue({ ok: true, value: { domains: undefined } } as never)
    const controller = new MemorySettingsController(scope as unknown as ConfigForm<MemorySettingsSection>, remote)
    await controller.inject().fetchDomains()
    expect(controller.inject().hooks.memorySettings.getSnapshot().data.domains).toEqual([])
  })

  it('reports a failed standalone domain fetch as a rejection', async () => {
    const { scope } = fakeScope(snapshot({}))
    const { remote, listDomains } = fakeRemote()
    listDomains.mockResolvedValue({ ok: false, error: { message: '领域查询失败' } } as never)
    const controller = new MemorySettingsController(scope as unknown as ConfigForm<MemorySettingsSection>, remote)
    await expect(controller.inject().fetchDomains()).rejects.toThrow('领域查询失败')
    expect(controller.inject().hooks.memorySettings.getSnapshot().lastError).toBe('领域查询失败')
  })

  it('keeps the current page on screen and reports why when a page fetch fails', async () => {
    const { scope } = fakeScope(snapshot({}))
    const { remote, listFacts } = fakeRemote()
    listFacts.mockResolvedValueOnce({
      ok: true,
      value: { facts: [{ fact_id: 'f1', subject: 's', predicate: 'p', object: 'o' }], total: 240 },
    } as never)
    const controller = new MemorySettingsController(scope as unknown as ConfigForm<MemorySettingsSection>, remote)
    const face = controller.inject()
    await face.refreshData()
    listFacts.mockResolvedValueOnce({ ok: false, error: { message: '桥接未就绪' } } as never)
    await expect(face.fetchFactsPage(50, 50)).rejects.toThrow('桥接未就绪')
    const snap = face.hooks.memorySettings.getSnapshot()
    // The failed page never lands: the rows already loaded stay put rather than
    // blanking the table, and the reason is on the snapshot.
    expect(snap.data.facts.map(f => f.fact_id)).toEqual(['f1'])
    expect(snap.lastError).toBe('桥接未就绪')
  })

  it('normalizes malformed remote list results to empty arrays (no blank-section crash)', async () => {
    const { scope } = fakeScope(snapshot({}))
    const { remote, listFacts, listProfile } = fakeRemote()
    listFacts.mockResolvedValue({ ok: true, value: { facts: undefined, total: 0 } } as never)
    listProfile.mockResolvedValue({ ok: true, value: { profile: undefined } } as never)
    const controller = new MemorySettingsController(scope as unknown as ConfigForm<MemorySettingsSection>, remote)
    await controller.inject().refreshData()
    const snap = controller.inject().hooks.memorySettings.getSnapshot()
    expect(snap.data.facts).toEqual([])
    expect(snap.data.profile).toEqual([])
  })

  it('deletes a fact through the Remote namespace and refreshes', async () => {
    const { scope } = fakeScope(snapshot({}))
    const { remote, deleteFact } = fakeRemote()
    const listFacts = (remote.listFacts as ReturnType<typeof vi.fn>)
    listFacts.mockResolvedValue({
      ok: true,
      value: { facts: [], total: 0 },
    })
    const controller = new MemorySettingsController(scope as unknown as ConfigForm<MemorySettingsSection>, remote)
    await controller.inject().deleteFact('f1')
    expect(deleteFact).toHaveBeenCalledWith({ user: 'global', fact_id: 'f1' })
  })

  it('fetches and stores the rendered summary view', async () => {
    const { scope } = fakeScope(snapshot({}))
    const { remote, summary } = fakeRemote()
    const controller = new MemorySettingsController(scope as unknown as ConfigForm<MemorySettingsSection>, remote)
    const face = controller.inject()
    const text = await face.fetchSummary()
    expect(summary).toHaveBeenCalledWith({ user: 'global' })
    expect(text).toBe('# 记忆摘要 (Summary) — global\n决策规则\n- 一条规则')
    expect(face.hooks.memorySettings.getSnapshot().data.summary).toBe('# 记忆摘要 (Summary) — global\n决策规则\n- 一条规则')
  })

  it('batch-saves a facts table: edits non-deleted rows, deletes marked rows, one refresh', async () => {
    const { scope } = fakeScope(snapshot({}))
    const { remote } = fakeRemote()
    const editFact = remote.editFact as ReturnType<typeof vi.fn>
    const deleteFact = remote.deleteFact as ReturnType<typeof vi.fn>
    const listFacts = remote.listFacts as ReturnType<typeof vi.fn>
    listFacts.mockResolvedValue({ ok: true, value: { facts: [], total: 0 } })
    const controller = new MemorySettingsController(scope as unknown as ConfigForm<MemorySettingsSection>, remote)
    await controller.inject().saveAllFacts([
      { fact_id: 'f1', subject: 'a', predicate: 'b', object: 'c', deleted: false },
      { fact_id: 'f2', subject: 'x', predicate: 'y', object: 'z', deleted: true },
    ])
    expect(editFact).toHaveBeenCalledWith(expect.objectContaining({ fact_id: 'f1' }))
    expect(deleteFact).toHaveBeenCalledWith({ user: 'global', fact_id: 'f2' })
    // After the batch, the panel refreshes (so one listFacts read happens again).
    expect(listFacts.mock.calls.length).toBeGreaterThan(0)
  })

  it('sends only the fields the user changed, so an untouched fact keeps its type', async () => {
    const { scope } = fakeScope(snapshot({}))
    const { remote } = fakeRemote()
    const editFact = remote.editFact as ReturnType<typeof vi.fn>
    const listFacts = remote.listFacts as ReturnType<typeof vi.fn>
    // The snapshot the editor opened with: this fact is an SOP, not a semantic
    // memory.
    listFacts.mockResolvedValue({
      ok: true,
      value: {
        facts: [{ fact_id: 'f1', subject: 'a', predicate: 'b', object: 'c', content: 'x', type: 'sop' }],
        total: 1,
      },
    })
    const controller = new MemorySettingsController(scope as unknown as ConfigForm<MemorySettingsSection>, remote)
    const face = controller.inject()
    await face.refreshData()
    await face.saveAllFacts([
      // Only `object` was edited.
      { fact_id: 'f1', subject: 'a', predicate: 'b', object: 'CHANGED', content: 'x', type: 'sop', deleted: false },
    ])
    expect(editFact).toHaveBeenCalledTimes(1)
    const patch = editFact.mock.calls[0][0] as Record<string, unknown>
    expect(patch).toEqual({ user: 'global', fact_id: 'f1', object: 'CHANGED' })
    // `type` in particular: sending it re-typed the row on every save, because
    // the editor never showed it and the draft carried an empty string.
    expect(patch).not.toHaveProperty('type')
    expect(patch).not.toHaveProperty('subject')
  })

  it('sends nothing at all when the user changed nothing', async () => {
    const { scope } = fakeScope(snapshot({}))
    const { remote } = fakeRemote()
    const editFact = remote.editFact as ReturnType<typeof vi.fn>
    const listFacts = remote.listFacts as ReturnType<typeof vi.fn>
    listFacts.mockResolvedValue({
      ok: true,
      value: { facts: [{ fact_id: 'f1', subject: 'a', predicate: 'b', object: 'c', content: 'x' }], total: 1 },
    })
    const controller = new MemorySettingsController(scope as unknown as ConfigForm<MemorySettingsSection>, remote)
    const face = controller.inject()
    await face.refreshData()
    await face.saveAllFacts([
      { fact_id: 'f1', subject: 'a', predicate: 'b', object: 'c', content: 'x', deleted: false },
    ])
    expect(editFact).not.toHaveBeenCalled()
  })

  it('rejects when a fact edit fails, so the panel can keep the editor open', async () => {
    const { scope } = fakeScope(snapshot({}))
    const { remote } = fakeRemote()
    const editFact = remote.editFact as ReturnType<typeof vi.fn>
    const listFacts = remote.listFacts as ReturnType<typeof vi.fn>
    listFacts.mockResolvedValue({
      ok: true,
      value: { facts: [{ fact_id: 'f1', subject: 'a', predicate: 'b', object: 'c', content: 'x' }], total: 1 },
    })
    editFact.mockResolvedValue({ ok: false, error: new Error('edit refused') })
    const controller = new MemorySettingsController(scope as unknown as ConfigForm<MemorySettingsSection>, remote)
    const face = controller.inject()
    await face.refreshData()
    await expect(
      face.saveAllFacts([
        { fact_id: 'f1', subject: 'a', predicate: 'b', object: 'CHANGED', content: 'x', deleted: false },
      ]),
    ).rejects.toThrow('edit refused')
    // ...and the reason is also on the snapshot for the panel to render.
    expect(face.hooks.memorySettings.getSnapshot().lastError).toBe('edit refused')
  })

  it('rejects when a fact deletion fails', async () => {
    const { scope } = fakeScope(snapshot({}))
    const { remote } = fakeRemote()
    const deleteFact = remote.deleteFact as ReturnType<typeof vi.fn>
    deleteFact.mockResolvedValue({ ok: false, error: new Error('delete refused') })
    const controller = new MemorySettingsController(scope as unknown as ConfigForm<MemorySettingsSection>, remote)
    await expect(
      controller.inject().saveAllFacts([
        { fact_id: 'f1', subject: 'a', predicate: 'b', object: 'c', deleted: true },
      ]),
    ).rejects.toThrow('delete refused')
  })

  it('batch-saves a profile table in one call: upserts and deletions together', async () => {
    const { scope } = fakeScope(snapshot({}))
    const { remote, writeProfile } = fakeRemote()
    const listProfile = remote.listProfile as ReturnType<typeof vi.fn>
    const controller = new MemorySettingsController(scope as unknown as ConfigForm<MemorySettingsSection>, remote)
    await controller.inject().saveAllProfile([
      { section: '背景', key: '职业', value: '工程师', deleted: false },
      { section: '偏好', key: '语言', value: 'Python', deleted: true },
    ])
    // One batch call, not a row-by-row loop: the store enforces the row cap on
    // the whole batch, so a refusal cannot leave half the edits applied.
    expect(writeProfile).toHaveBeenCalledTimes(1)
    expect(writeProfile).toHaveBeenCalledWith({
      user: 'global',
      rows: [
        { section: '背景', key: '职业', value: '工程师', deleted: false },
        { section: '偏好', key: '语言', value: 'Python', deleted: true },
      ],
    })
    expect(listProfile.mock.calls.length).toBeGreaterThan(0)
  })

  it('rethrows a refused profile save so the editor can stay open', async () => {
    const { scope } = fakeScope(snapshot({}))
    const { remote, writeProfile } = fakeRemote()
    writeProfile.mockResolvedValueOnce({ ok: false, error: { message: '用户画像已达上限（50/50 条）' } })
    const controller = new MemorySettingsController(scope as unknown as ConfigForm<MemorySettingsSection>, remote)
    const face = controller.inject()

    await expect(face.saveAllProfile([{ section: '背景', key: '职业', value: '工程师' }]))
      .rejects.toThrow(/已达上限/u)
    expect(face.hooks.memorySettings.getSnapshot().lastError).toContain('已达上限')
  })

  it('forwards a profile generation run and normalises its result', async () => {
    const { scope } = fakeScope(snapshot({}))
    const { remote, generateProfile } = fakeRemote()
    generateProfile.mockResolvedValueOnce({
      ok: true,
      value: {
        suggestions: [{ section: '职业', key: 'value', value: '工程师' }],
        existing: 3,
        limit: 50,
        full: false,
      },
    })
    const controller = new MemorySettingsController(scope as unknown as ConfigForm<MemorySettingsSection>, remote)
    const result = await controller.inject().generateProfile()

    expect(generateProfile).toHaveBeenCalledWith({ user: 'global' })
    expect(result.suggestions).toEqual([{ section: '职业', key: 'value', value: '工程师' }])
    expect(result.existing).toBe(3)
    expect(result.limit).toBe(50)
    expect(result.full).toBe(false)
  })

  it('reports a malformed generation result as an empty, non-full proposal', async () => {
    const { scope } = fakeScope(snapshot({}))
    const { remote, generateProfile } = fakeRemote()
    generateProfile.mockResolvedValueOnce({ ok: true, value: {} })
    const controller = new MemorySettingsController(scope as unknown as ConfigForm<MemorySettingsSection>, remote)
    const result = await controller.inject().generateProfile()
    expect(result.suggestions).toEqual([])
    expect(result.full).toBe(false)
  })

  it('carries a single profile edit without any pin flag', async () => {
    const { scope } = fakeScope(snapshot({}))
    const { remote } = fakeRemote()
    const upsertProfile = remote.upsertProfile as ReturnType<typeof vi.fn>
    const controller = new MemorySettingsController(scope as unknown as ConfigForm<MemorySettingsSection>, remote)
    const face = controller.inject()

    await face.upsertProfile('背景', '职业', '工程师')
    expect(upsertProfile).toHaveBeenLastCalledWith({
      user: 'global', section: '背景', key: '职业', value: '工程师',
    })
  })

  // -- the work-overview controls ---------------------------------------------

  it('routes setOverviewEnabled through the settings scope', async () => {
    const { scope, set } = fakeScope(snapshot({}))
    const { remote } = fakeRemote()
    const controller = new MemorySettingsController(scope as unknown as ConfigForm<MemorySettingsSection>, remote)
    await controller.inject().setOverviewEnabled(false)
    expect(set).toHaveBeenLastCalledWith('overviewEnabled', false)
  })

  it('defaults a missing overview switch to on', () => {
    // Older settings documents predate the field; the panel must not render it
    // as unchecked and imply the feature is off when it is running.
    const value = { ...snapshot({}).value } as Partial<MemorySettingsSection>
    delete value.overviewEnabled
    const { scope } = fakeScope({ ...snapshot({}), value: value as MemorySettingsSection })
    const face = new MemorySettingsController(
      scope as unknown as ConfigForm<MemorySettingsSection>, fakeRemote().remote,
    ).inject()
    expect(face.hooks.memorySettings.getSnapshot().section.overviewEnabled).toBe(true)
  })

  it('forwards a refresh and returns the outcome token', async () => {
    const { scope } = fakeScope(snapshot({}))
    const { remote, refreshOverview } = fakeRemote()
    const face = new MemorySettingsController(
      scope as unknown as ConfigForm<MemorySettingsSection>, remote,
    ).inject()
    await expect(face.refreshOverview()).resolves.toBe('refreshed')
    expect(refreshOverview).toHaveBeenLastCalledWith({ user: 'global' })
  })

  it('drops the cached summary after a refresh, so the modal cannot go stale', async () => {
    // Without this the panel would keep rendering the pre-refresh overview and
    // read as "the button did nothing".
    const { scope } = fakeScope(snapshot({}))
    const { remote } = fakeRemote()
    const face = new MemorySettingsController(
      scope as unknown as ConfigForm<MemorySettingsSection>, remote,
    ).inject()
    await face.fetchSummary()
    expect(face.hooks.memorySettings.getSnapshot().data.summary).toBeTruthy()

    await face.refreshOverview()
    expect(face.hooks.memorySettings.getSnapshot().data.summary).toBeUndefined()
  })

  it('surfaces a failed refresh as a rejection', async () => {
    const { scope } = fakeScope(snapshot({}))
    const { remote, refreshOverview } = fakeRemote()
    refreshOverview.mockResolvedValueOnce({ ok: false, error: { message: 'bridge down' } })
    const face = new MemorySettingsController(
      scope as unknown as ConfigForm<MemorySettingsSection>, remote,
    ).inject()
    await expect(face.refreshOverview()).rejects.toThrow(/bridge down/)
  })
})
