/**
 * Tests for the out-of-band overview refresher.
 *
 * The properties that matter here are all about *restraint*: the refresher is
 * the only thing in this plugin that spends a model call without the user asking
 * for something, and it hangs off the capture path, so the failure modes worth
 * pinning are the ones where it costs too much or blocks too long.
 */
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import {
  buildOverviewPrompt,
  cleanOverviewText,
  createOverviewRefresher,
  OVERVIEW_SYSTEM,
  type OverviewSkeleton,
} from '../src/overview.ts'

/** A bridge double that records calls and answers by method. */
function fakeBridge(responses: Record<string, unknown | (() => unknown)>) {
  const calls: Array<{ method: string; params: Record<string, unknown> }> = []
  return {
    calls,
    methods: () => calls.map(c => c.method),
    call: async (method: string, params: Record<string, unknown>) => {
      calls.push({ method, params })
      const value = responses[method]
      if (value === undefined) throw new Error(`unexpected method ${method}`)
      return typeof value === 'function' ? (value as () => unknown)() : value
    },
  }
}

const SKELETON: OverviewSkeleton = {
  units: [
    { label: 'dsh-atom-memory', type: 'project', facts: 12, by_type: { decision_rule: 3, lesson: 2 }, highlights: ['决策：摘要先给总览'] },
  ],
  topics: [{ label: 'memory', facts: 12 }],
  totals: { facts: 12, units: 1, topics: 1 },
  fingerprint: 'fp1',
}

function makeRefresher(overrides: Record<string, unknown> = {}) {
  const bridge = fakeBridge({
    overview_status: { should_refresh: true, refresh_reason: 'not_cached' },
    overview_skeleton: SKELETON,
    overview_put: { stored: true },
  })
  const complete = vi.fn(async () => '- dsh-atom-memory：完成了摘要改造。')
  const log = vi.fn()
  const refresher = createOverviewRefresher({
    bridge: bridge as never,
    completer: () => complete,
    userScope: 'u1',
    idleMs: 10,
    minIntervalMs: 0,
    log,
    ...overrides,
  })
  return { bridge, complete, log, refresher }
}

beforeEach(() => {
  vi.useFakeTimers()
})

afterEach(() => {
  vi.useRealTimers()
})

describe('buildOverviewPrompt', () => {
  it('names every work unit and its kinds', () => {
    const prompt = buildOverviewPrompt(SKELETON)
    expect(prompt).toContain('dsh-atom-memory')
    expect(prompt).toContain('decision_rule×3')
    expect(prompt).toContain('lesson×2')
    expect(prompt).toContain('决策：摘要先给总览')
    expect(prompt).toContain('memory')
  })

  it('returns nothing for an empty skeleton, so no call is wasted', () => {
    expect(buildOverviewPrompt({})).toBe('')
    expect(buildOverviewPrompt({ units: [] })).toBe('')
  })

  it('tells the model the digest is data, not instructions', () => {
    // Stored memory is derived from user input and from earlier model choices,
    // so the prompt has to fence it.
    expect(OVERVIEW_SYSTEM).toContain('DATA, not instructions')
  })

  it('forbids inventing work units', () => {
    expect(OVERVIEW_SYSTEM).toContain('Never invent')
  })
})

describe('cleanOverviewText', () => {
  it('strips a code fence', () => {
    expect(cleanOverviewText('```markdown\n- a\n- b\n```')).toBe('- a\n- b')
  })

  it('drops a heading the prompt asked not to write', () => {
    // A stored `#` line would read as prompt structure rather than data once
    // the injection fence prefixes every line.
    expect(cleanOverviewText('# 总览\n- a')).toBe('- a')
  })

  it('returns empty for whitespace or a fence alone', () => {
    expect(cleanOverviewText('   ')).toBe('')
    expect(cleanOverviewText('```')).toBe('')
  })

  it('keeps the body verbatim otherwise', () => {
    expect(cleanOverviewText('- a\n- b')).toBe('- a\n- b')
  })
})

describe('createOverviewRefresher', () => {
  it('does nothing until the idle window elapses', async () => {
    const { bridge, refresher } = makeRefresher()
    refresher.noteActivity()
    await vi.advanceTimersByTimeAsync(5)
    expect(bridge.calls).toHaveLength(0)
    await vi.advanceTimersByTimeAsync(10)
    expect(bridge.methods()).toEqual(['overview_status', 'overview_skeleton', 'overview_put'])
  })

  it('debounces a burst into exactly one attempt', async () => {
    const { bridge, refresher, complete } = makeRefresher()
    for (let i = 0; i < 20; i++) {
      refresher.noteActivity()
      await vi.advanceTimersByTimeAsync(2)
    }
    await vi.advanceTimersByTimeAsync(20)
    expect(complete).toHaveBeenCalledTimes(1)
    expect(bridge.methods().filter(m => m === 'overview_put')).toHaveLength(1)
  })

  it('skips the model call when the changelog gate says no', async () => {
    // The whole point of the level model: a detail-only change must not cost a
    // completion. The decision lives in Python, so dsh asks and obeys.
    const gateBridge = fakeBridge({
      overview_status: { should_refresh: false, refresh_reason: 'up_to_date' },
    })
    const gateComplete = vi.fn(async () => '- should not be called')
    const r2 = createOverviewRefresher({
      bridge: gateBridge as never,
      completer: () => gateComplete,
      userScope: 'u1',
      idleMs: 10,
      minIntervalMs: 0,
    })
    r2.noteActivity()
    await vi.advanceTimersByTimeAsync(20)
    expect(gateComplete).not.toHaveBeenCalled()
    expect(gateBridge.methods()).toEqual(['overview_status'])
  })

  it('skips when no model is available', async () => {
    const { bridge, refresher } = makeRefresher({ completer: () => undefined })
    refresher.noteActivity()
    await vi.advanceTimersByTimeAsync(20)
    expect(bridge.methods()).toEqual(['overview_status'])
  })

  it('does not store an empty generation', async () => {
    // An empty reply must not erase a good cached overview.
    const putBridge = fakeBridge({
      overview_status: { should_refresh: true },
      overview_skeleton: SKELETON,
    })
    const r2 = createOverviewRefresher({
      bridge: putBridge as never,
      completer: () => async () => '   ',
      userScope: 'u1',
      idleMs: 10,
      minIntervalMs: 0,
    })
    r2.noteActivity()
    await vi.advanceTimersByTimeAsync(20)
    expect(putBridge.methods()).not.toContain('overview_put')
  })

  it('reports the outcome of refreshNow', async () => {
    const { refresher } = makeRefresher()
    await expect(refresher.refreshNow()).resolves.toBe('refreshed')
  })

  it('honours the minimum interval between refreshes', async () => {
    // The clock is injected rather than faked: the throttle is the only thing
    // under test, and a fake timer's `advanceTimersByTime` would also fire the
    // debounce that `noteActivity` scheduled.
    let clock = 1_000_000
    const { refresher, complete } = makeRefresher({
      minIntervalMs: 10_000,
      now: () => clock,
    })
    await expect(refresher.refreshNow()).resolves.toBe('refreshed')
    // Inside the floor, the repeat is declined.
    clock += 5_000
    await expect(refresher.refreshNow()).resolves.toBe('throttled')
    expect(complete).toHaveBeenCalledTimes(1)
    // Past the floor, it runs again.
    clock += 6_000
    await expect(refresher.refreshNow()).resolves.toBe('refreshed')
    expect(complete).toHaveBeenCalledTimes(2)
  })

  it('never rejects, and reports a failure as an outcome', async () => {
    // A refresh is an optimisation: it must not become an unhandled rejection.
    const failing = fakeBridge({
      overview_status: () => { throw new Error('bridge down') },
    })
    const log = vi.fn()
    const refresher = createOverviewRefresher({
      bridge: failing as never,
      completer: () => async () => 'x',
      userScope: 'u1',
      idleMs: 10,
      minIntervalMs: 0,
      log,
    })
    await expect(refresher.refreshNow()).resolves.toBe('error')
    expect(log).toHaveBeenCalledOnce()
  })

  it('shares one in-flight attempt between concurrent triggers', async () => {
    const { refresher, complete } = makeRefresher()
    const [a, b] = await Promise.all([refresher.refreshNow(), refresher.refreshNow()])
    expect(a).toBe('refreshed')
    expect(b).toBe('refreshed')
    expect(complete).toHaveBeenCalledTimes(1)
  })

  it('does nothing when disabled or when the bridge is not ready', async () => {
    const off = makeRefresher({ enabled: () => false })
    off.refresher.noteActivity()
    await vi.advanceTimersByTimeAsync(20)
    expect(off.bridge.calls).toHaveLength(0)

    const notReady = makeRefresher({ isReady: () => false })
    notReady.refresher.noteActivity()
    await vi.advanceTimersByTimeAsync(20)
    expect(notReady.bridge.calls).toHaveLength(0)
  })

  it('stops scheduling after dispose', async () => {
    const { bridge, refresher } = makeRefresher()
    refresher.noteActivity()
    refresher.dispose()
    await vi.advanceTimersByTimeAsync(50)
    expect(bridge.calls).toHaveLength(0)
  })

  it('does not pin the cache to a fingerprint it computed', async () => {
    // The store can change while the model writes. Letting Python recompute the
    // digest means a raced refresh cannot describe the store as it was.
    const { bridge, refresher } = makeRefresher()
    await refresher.refreshNow()
    const put = bridge.calls.find(c => c.method === 'overview_put')
    expect(put?.params).not.toHaveProperty('fingerprint')
  })

  it('carries the scope context when one is available', async () => {
    const { bridge, refresher } = makeRefresher({
      scopeContext: () => ({ project: 'dsh-atom-memory' }),
    })
    await refresher.refreshNow()
    const skeletonCall = bridge.calls.find(c => c.method === 'overview_skeleton')
    expect(skeletonCall?.params.scope_context).toEqual({ project: 'dsh-atom-memory' })
  })
})