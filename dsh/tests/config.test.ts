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
    const c = Config({})
    expect(c.contextInjectionEnabled).toBe(true)
    expect(c.summaryTokens).toBe(1500)
  })

  it('declares no extra multi-valued predicates by default', () => {
    expect(Config({}).multiValuedPredicates).toEqual([])
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
})
