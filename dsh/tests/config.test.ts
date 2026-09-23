import { describe, it, expect } from 'vitest'
import { Config } from '../src/config.ts'
import { buildStartParams } from '../src/index.ts'

describe('Config defaults', () => {
  it('defaults extractionMaxTokens to a budget that fits a knowledge body', () => {
    // 600 (the old hard-coded value) truncated long knowledge; a truncated
    // extraction is discarded rather than persisted, losing the fact silently.
    expect(Config({}).extractionMaxTokens).toBe(2048)
  })

  it('keeps snapshot injection on and summary budget at 1500 by default', () => {
    // The live fields are `.volatile()`, so their value is read through the
    // reference's `get()` — that indirection is what lets the settings panel
    // update them in place. Asserting on the raw field would compare a
    // reference object to a boolean and pass for the wrong reason.
    const c = Config({})
    expect(c.contextInjectionEnabled.get()).toBe(true)
    expect(c.summaryTokens).toBe(1500)
  })

  it('declares no extra multi-valued predicates by default', () => {
    expect(Config({}).multiValuedPredicates).toEqual([])
  })
})

describe('volatile surface', () => {
  // The panel's own existence depends on this: DSH builds a plugin's settings
  // page from the Config's volatile nodes, and a Config with none produces no
  // page at all — the namespace is dropped from the served list, and every
  // control the browser panel guards renders disabled. So the set of volatile
  // fields is a contract, not an implementation detail.
  const isRef = (value: unknown): boolean =>
    typeof value === 'object' && value !== null && typeof (value as { get?: unknown }).get === 'function'

  it('exposes every runtime-editable field as a volatile reference', () => {
    const c = Config({})
    for (const field of [
      'extractionModel', 'captureEnabled', 'llmExtractionEnabled',
      'injectedSummaryTokens', 'contextInjectionEnabled', 'overviewEnabled',
    ] as const) {
      expect(isRef(c[field]), `${field} must be volatile`).toBe(true)
    }
  })

  it('keeps deploy-time fields as plain values, not references', () => {
    // Marking one of these volatile would offer a live editor for a value the
    // bridge only reads when it is spawned, so an edit would silently do
    // nothing until the next restart.
    const c = Config({})
    for (const field of ['dbPath', 'pythonBin', 'autostart', 'rpcTimeoutMs'] as const) {
      expect(isRef(c[field]), `${field} must NOT be volatile`).toBe(false)
    }
  })
})

describe('start params', () => {
  it('leaves multi_valued_predicates out when the deployment declares none', () => {
    // The wire shape is the compatibility surface: an unconfigured deployment
    // must send exactly the params it sent before the field existed.
    expect('multi_valued_predicates' in buildStartParams(Config({}))).toBe(false)
  })

  it('forwards declared multi-valued predicates to the store', () => {
    const params = buildStartParams(Config({ multiValuedPredicates: ['在研课题'] }))
    expect(params.multi_valued_predicates).toEqual(['在研课题'])
  })

  it('ships a non-zero relevance floor by default', () => {
    // At 0 the floor never fires, so recall always answers with a full
    // maxRecalledFacts of rows: a large store becomes indistinguishable from a
    // tiny one, and the weakest single-list matches reach the context budget.
    expect(Config({}).minRelevance).toBeGreaterThan(0)
    expect(buildStartParams(Config({})).min_relevance).toBe(Config({}).minRelevance)
  })

  it('forwards the candidate pool multiplier', () => {
    // Decoupled from the number of results the caller sees: the two retrieval
    // legs must look deeper than top_k or a fact outside both top-k lists can
    // never be fused, whatever its ranking terms say.
    expect(Config({}).candidatePoolMultiplier).toBe(4)
    expect(buildStartParams(Config({})).candidate_pool_multiplier).toBe(4)
  })

  it('forwards the recency half-life', () => {
    // How much "recent" is worth in the re-rank. Kept configurable so a store
    // whose facts turn over in days is not stuck with a durable-knowledge curve.
    expect(Config({}).recencyHalfLifeDays).toBe(30)
    expect(buildStartParams(Config({})).recency_half_life_days).toBe(30)
    expect(
      buildStartParams(Config({ recencyHalfLifeDays: 7 })).recency_half_life_days,
    ).toBe(7)
  })

  it('omits the recency window so the library derives it', () => {
    // 0 means "derive 3x the half-life". Sending an explicit 0 would pin the
    // window at zero and flatten every age onto one credit.
    expect(Config({}).recencyReferenceWindowDays).toBe(0)
    expect('recency_reference_window_days' in buildStartParams(Config({}))).toBe(false)
    expect(
      buildStartParams(Config({ recencyReferenceWindowDays: 10 }))
        .recency_reference_window_days,
    ).toBe(10)
  })
})
