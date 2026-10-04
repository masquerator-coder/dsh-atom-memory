/**
 * Tests for the out-of-band topic-queue promoter.
 *
 * The properties worth pinning are the ones the module exists for. It is the
 * second thing in this plugin that spends a model call without being asked, and
 * it writes permanent vocabulary, so the failure modes are: promoting noise,
 * promoting too much at once, letting the model widen the set, losing the queue
 * when a name is unstorable, and blocking the write path.
 */
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import {
  aggregateSignals,
  buildPromotionPrompt,
  createDomainPromoter,
  parsePromotionReply,
  PROMOTION_SYSTEM,
} from '../src/domain-promotion.ts'

/** A bridge double that records calls and answers by method. */
function fakeBridge(responses: Record<string, unknown | (() => unknown)>) {
  const calls: Array<{ method: string; params: Record<string, unknown> }> = []
  return {
    calls,
    methods: () => calls.map(c => c.method),
    promotedNames: () => calls
      .filter(c => c.method === 'domain_signal_promote')
      .map(c => String(c.params.name)),
    call: async (method: string, params: Record<string, unknown>) => {
      calls.push({ method, params })
      const value = responses[method]
      if (value === undefined) throw new Error(`unexpected method ${method}`)
      return typeof value === 'function' ? (value as () => unknown)() : value
    },
  }
}

const QUEUE = [
  { name: 'research', seen_count: 74 },
  { name: 'teaching', seen_count: 58 },
  { name: 'life/travel', seen_count: 1 },
]

function makePromoter(overrides: Record<string, unknown> = {}) {
  const bridge = fakeBridge({
    domain_unresolved: QUEUE,
    domain_signal_promote: { domain_id: 1, name: 'x', already_registered: false },
  })
  const complete = vi.fn(async (_system: string, _user: string): Promise<string> => '["research","teaching"]')
  const log = vi.fn()
  const promoter = createDomainPromoter({
    bridge: bridge as never,
    completer: () => complete,
    userScope: 'u1',
    idleMs: 10,
    minIntervalMs: 0,
    threshold: 3,
    log,
    ...overrides,
  })
  return { bridge, complete, log, promoter }
}

describe('domain promotion queue gating', () => {
  it('only offers proposals that reached the threshold', async () => {
    const { bridge, complete, promoter } = makePromoter()

    const outcome = await promoter.promoteNow()

    expect(outcome).toBe('promoted:2')
    // `life/travel` (1 sighting) must never reach the model.
    const prompt = complete.mock.calls[0]?.[1] ?? ''
    expect(prompt).toContain('research')
    expect(prompt).toContain('teaching')
    expect(prompt).not.toContain('life/travel')
    expect(bridge.promotedNames()).toEqual(['research', 'teaching'])
  })

  it('reports below-threshold and never calls the model', async () => {
    const { complete, promoter } = makePromoter({
      threshold: 100,
    })

    expect(await promoter.promoteNow()).toBe('below-threshold')
    expect(complete).not.toHaveBeenCalled()
  })

  it('caps how many topics one run registers, taking the most frequent first', async () => {
    const bridge = fakeBridge({
      domain_unresolved: [
        { name: 'a', seen_count: 5 },
        { name: 'b', seen_count: 50 },
        { name: 'c', seen_count: 20 },
      ],
      domain_signal_promote: { domain_id: 1, name: 'x' },
    })
    const complete = vi.fn(async (_system: string, _user: string): Promise<string> => '["b","c"]')
    const promoter = createDomainPromoter({
      bridge: bridge as never,
      completer: () => complete,
      userScope: 'u1',
      idleMs: 10,
      minIntervalMs: 0,
      threshold: 1,
      maxPerRun: 2,
      log: () => {},
    })

    await promoter.promoteNow()

    // Highest count first, so a capped run spends its slots on the topics the
    // user recurs on most and the rest arrive on later runs. `a` (5) loses the
    // slot to `c` (20) — that exclusion is the cap doing its job.
    const prompt = complete.mock.calls[0]?.[1] ?? ''
    expect(prompt.indexOf('b (50 times)')).toBeLessThan(prompt.indexOf('c (20 times)'))
    expect(prompt).not.toContain('a (5 times)')
    expect(bridge.promotedNames()).toEqual(['b', 'c'])
  })
})

describe('domain promotion model authority', () => {
  it('never registers a name the model invented', async () => {
    const { bridge, promoter } = makePromoter({
      completer: () => vi.fn(async () => '["research","totally-made-up","teaching"]'),
    })

    expect(await promoter.promoteNow()).toBe('promoted:2')
    expect(bridge.promotedNames()).toEqual(['research', 'teaching'])
  })

  it('rejects off-list names at the parse boundary', async () => {
    // Pinned directly rather than only through the promoter, because the
    // candidate-order return at the end of `parsePromotionReply` filters
    // off-list names on its own — through the promoter alone, this guard and
    // that one are indistinguishable, so a mutant of either survives.
    expect(parsePromotionReply('["teaching","invented"]', ['teaching'])).toEqual(['teaching'])
    expect(parsePromotionReply('["invented"]', ['teaching'])).toEqual([])
  })

  it('drops a re-spelled candidate rather than creating a second topic', async () => {
    const { bridge, promoter } = makePromoter({
      completer: () => vi.fn(async () => '["Research","teaching"]'),
    })

    await promoter.promoteNow()

    expect(bridge.promotedNames()).toEqual(['teaching'])
  })

  it('degrades to nothing when the model approves none', async () => {
    const { bridge, promoter } = makePromoter({
      completer: () => vi.fn(async () => '[]'),
    })

    expect(await promoter.promoteNow()).toBe('none-approved')
    expect(bridge.promotedNames()).toEqual([])
  })

  it('degrades to nothing when the model is unavailable', async () => {
    const { bridge, promoter } = makePromoter({ completer: () => undefined })

    expect(await promoter.promoteNow()).toBe('no-model')
    expect(bridge.promotedNames()).toEqual([])
  })

  it('is not asked to reject anything', async () => {
    const { bridge, promoter } = makePromoter()

    await promoter.promoteNow()

    // Approval only: rejection stays a human action, so a bad judgement costs an
    // extra topic the user can archive rather than a silently discarded one.
    expect(bridge.methods()).not.toContain('domain_signal_reject')
    expect(PROMOTION_SYSTEM).not.toMatch(/reject/i)
  })
})

describe('domain promotion failure isolation', () => {
  it('keeps promoting after one name is unstorable', async () => {
    // A local-path proposal carries a colon and is rejected by Python; it must
    // not abort the names after it.
    const bridge = fakeBridge({
      domain_unresolved: [
        { name: 'c:/users/x', seen_count: 9 },
        { name: 'teaching', seen_count: 8 },
      ],
      domain_signal_promote: (() => {
        let n = 0
        return () => {
          n += 1
          if (n === 1) throw new Error("invalid domain name: 'c:/users/x'")
          return { domain_id: 2, name: 'teaching' }
        }
      })(),
    })
    const complete = vi.fn(async () => '["c:/users/x","teaching"]')
    const log = vi.fn()
    const promoter = createDomainPromoter({
      bridge: bridge as never,
      completer: () => complete,
      userScope: 'u1',
      idleMs: 10,
      minIntervalMs: 0,
      threshold: 1,
      log,
    })

    const outcome = await promoter.promoteNow()

    expect(outcome).toBe('promoted:1(failed:1)')
    expect(bridge.promotedNames()).toEqual(['c:/users/x', 'teaching'])
    expect(log).toHaveBeenCalled()
  })

  it('reports a failed run without throwing', async () => {
    const bridge = fakeBridge({
      domain_unresolved: () => { throw new Error('bridge down') },
    })
    const log = vi.fn()
    const promoter = createDomainPromoter({
      bridge: bridge as never,
      completer: () => vi.fn(async () => '[]'),
      userScope: 'u1',
      idleMs: 10,
      minIntervalMs: 0,
      log,
    })

    await expect(promoter.promoteNow()).resolves.toBe('error')
    expect(log).toHaveBeenCalled()
  })
})

describe('domain promotion restraint', () => {
  it('does nothing when disabled', async () => {
    const { bridge, complete, promoter } = makePromoter({ enabled: () => false })

    expect(await promoter.promoteNow()).toBe('skipped')
    expect(bridge.calls).toEqual([])
    expect(complete).not.toHaveBeenCalled()
  })

  it('does nothing when the bridge is not ready', async () => {
    const { bridge, promoter } = makePromoter({ isReady: () => false })

    expect(await promoter.promoteNow()).toBe('skipped')
    expect(bridge.calls).toEqual([])
  })

  it('throttles a second run inside the minimum gap', async () => {
    let clock = 1_000
    const { bridge, promoter } = makePromoter({
      minIntervalMs: 30 * 60_000,
      now: () => clock,
    })

    expect(await promoter.promoteNow()).toBe('promoted:2')
    const afterFirst = bridge.calls.length
    clock += 60_000
    expect(await promoter.promoteNow()).toBe('throttled')
    expect(bridge.calls.length).toBe(afterFirst)
  })

  it('shares one run between concurrent triggers, and one per burst', async () => {
    vi.useFakeTimers()
    try {
      const { bridge, promoter } = makePromoter()
      promoter.noteActivity()
      promoter.noteActivity()
      promoter.noteActivity()
      await vi.advanceTimersByTimeAsync(20)
      // Three notes inside the idle window are one run at the pause.
      expect(bridge.methods().filter(m => m === 'domain_unresolved')).toHaveLength(1)
    } finally {
      vi.useRealTimers()
    }
  })
})

describe('domain promotion signal aggregation', () => {
  // The real store's shape: the queue is keyed per scope, so one name appears
  // many times with different counts. These counts are copied from the live
  // database (`teaching` 7 rows, `tooling` 9) rather than invented, because a
  // fixture that does not match the real format cannot catch this class of bug.
  const REAL_SHAPE = [
    { name: 'teaching', seen_count: 58 },
    { name: 'teaching', seen_count: 15 },
    { name: 'teaching', seen_count: 11 },
    { name: 'teaching', seen_count: 4 },
    { name: 'teaching', seen_count: 2 },
    { name: 'teaching', seen_count: 2 },
    { name: 'teaching', seen_count: 1 },
    { name: 'research', seen_count: 74 },
    { name: 'research', seen_count: 11 },
    { name: 'research', seen_count: 2 },
    { name: 'ui', seen_count: 2 },
    { name: 'ui', seen_count: 1 },
    { name: 'ui', seen_count: 1 },
  ]

  it('sums a name\'s sightings across its per-scope rows', () => {
    const out = aggregateSignals(REAL_SHAPE, 3)

    // `ui` sums to 4, so it qualifies too — the point here is the totals and
    // the descending order, not which names survive.
    expect(out).toEqual([
      { name: 'teaching', seen_count: 93 },
      { name: 'research', seen_count: 87 },
      { name: 'ui', seen_count: 4 },
    ])
  })

  it('qualifies a name whose rows are each below the threshold but which sum above it', () => {
    // `ui` is 2+1+1 = 4 sightings across three rows. Judged row by row it would
    // be discarded as three singletons, which is exactly the bug this prevents.
    const out = aggregateSignals(REAL_SHAPE, 4)

    expect(out.map(r => r.name)).toContain('ui')
    expect(out.find(r => r.name === 'ui')?.seen_count).toBe(4)
  })

  it('emits one entry per name, so a run cannot spend all its slots on one topic', () => {
    const out = aggregateSignals(REAL_SHAPE, 1)

    expect(out).toHaveLength(3)
    expect(out.filter(r => r.name === 'teaching')).toHaveLength(1)
  })

  it('ignores blank names', () => {
    const out = aggregateSignals(
      [{ name: '  ', seen_count: 50 }, { name: undefined, seen_count: 9 }, { name: 'ok', seen_count: 5 }],
      1,
    )

    expect(out).toEqual([{ name: 'ok', seen_count: 5 }])
  })

  it('orders deterministically when counts tie', () => {
    const tied = [{ name: 'b', seen_count: 5 }, { name: 'a', seen_count: 5 }, { name: 'c', seen_count: 5 }]

    expect(aggregateSignals(tied, 1).map(r => r.name)).toEqual(['a', 'b', 'c'])
    // Same input, same order — a run's selection must not vary between runs.
    expect(aggregateSignals(tied, 1)).toEqual(aggregateSignals(tied, 1))
  })
})

describe('domain promotion prompt and reply', () => {
  it('renders each candidate with its sighting count', () => {
    const prompt = buildPromotionPrompt([
      { name: 'teaching', seen_count: 58 },
      { name: 'research', seen_count: 74 },
    ])

    expect(prompt).toContain('1. teaching (58 times)')
    expect(prompt).toContain('2. research (74 times)')
  })

  it('returns nothing for an empty candidate list', () => {
    expect(buildPromotionPrompt([])).toBe('')
    expect(buildPromotionPrompt([{ name: '   ', seen_count: 5 }])).toBe('')
  })

  it('parses a fenced reply', () => {
    expect(parsePromotionReply('```json\n["teaching"]\n```', ['teaching', 'x']))
      .toEqual(['teaching'])
  })

  it('returns nothing for a malformed reply', () => {
    for (const raw of ['', 'no json here', '["teaching"', '{not: an array}']) {
      expect(parsePromotionReply(raw, ['teaching'])).toEqual([])
    }
  })

  it('returns approved names in candidate order regardless of reply order', () => {
    expect(parsePromotionReply('["b","a"]', ['a', 'b', 'c'])).toEqual(['a', 'b'])
  })
})

describe('domain promotion idle debounce', () => {
  beforeEach(() => { vi.useFakeTimers() })
  afterEach(() => { vi.useRealTimers() })

  it('never runs before the idle window elapses', async () => {
    const { bridge, promoter } = makePromoter({ idleMs: 5_000 })

    promoter.noteActivity()
    await vi.advanceTimersByTimeAsync(4_000)

    expect(bridge.calls).toEqual([])
  })

  it('stops a scheduled run after dispose', async () => {
    const { bridge, promoter } = makePromoter({ idleMs: 1_000 })

    promoter.noteActivity()
    promoter.dispose()
    await vi.advanceTimersByTimeAsync(5_000)

    expect(bridge.calls).toEqual([])
  })
})