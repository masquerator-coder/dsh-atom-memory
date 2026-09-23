import { describe, it, expect } from 'vitest'
import { createRuntime } from '../src/runtime.ts'
import {
  DEFAULT_INJECTED_SUMMARY_TOKENS,
  INJECTED_SUMMARY_TOKEN_PRESETS,
  MAX_INJECTED_SUMMARY_TOKENS,
  MIN_INJECTED_SUMMARY_TOKENS,
  clampInjectedSummaryTokens,
  nearestInjectedSummaryPresetIndex,
} from '../src/injection-budget.ts'

describe('createRuntime', () => {
  it('applies defaults when the seed is empty', () => {
    const rt = createRuntime({})
    expect(rt).toMatchObject({
      captureEnabled: true,
      llmExtractionEnabled: true,
      contextInjectionEnabled: true,
      injectedSummaryTokens: DEFAULT_INJECTED_SUMMARY_TOKENS,
    })
  })

  it('carries an extractionModel override when provided', () => {
    const rt = createRuntime({ extractionModel: { provider: 'p', model: 'm' } })
    expect(rt.extractionModel).toEqual({ provider: 'p', model: 'm' })
  })

  it('carries a configured injection budget, clamped', () => {
    expect(createRuntime({ injectedSummaryTokens: 1200 }).injectedSummaryTokens).toBe(1200)
    expect(createRuntime({ injectedSummaryTokens: 0 }).injectedSummaryTokens)
      .toBe(MIN_INJECTED_SUMMARY_TOKENS)
  })
})

describe('clampInjectedSummaryTokens', () => {
  it('keeps an in-range integer as-is', () => {
    expect(clampInjectedSummaryTokens(800)).toBe(800)
    expect(clampInjectedSummaryTokens(1200.7)).toBe(1200)
  })

  it('snaps out-of-range values to the nearest bound', () => {
    expect(clampInjectedSummaryTokens(MIN_INJECTED_SUMMARY_TOKENS - 1)).toBe(MIN_INJECTED_SUMMARY_TOKENS)
    expect(clampInjectedSummaryTokens(0)).toBe(MIN_INJECTED_SUMMARY_TOKENS)
    expect(clampInjectedSummaryTokens(-100)).toBe(MIN_INJECTED_SUMMARY_TOKENS)
    expect(clampInjectedSummaryTokens(MAX_INJECTED_SUMMARY_TOKENS + 1)).toBe(MAX_INJECTED_SUMMARY_TOKENS)
  })

  it('falls back to the default for anything unusable', () => {
    // A malformed settings document must not break prompt assembly.
    expect(clampInjectedSummaryTokens(undefined)).toBe(DEFAULT_INJECTED_SUMMARY_TOKENS)
    expect(clampInjectedSummaryTokens(null)).toBe(DEFAULT_INJECTED_SUMMARY_TOKENS)
    expect(clampInjectedSummaryTokens('abc')).toBe(DEFAULT_INJECTED_SUMMARY_TOKENS)
    expect(clampInjectedSummaryTokens(Number.NaN)).toBe(DEFAULT_INJECTED_SUMMARY_TOKENS)
    expect(clampInjectedSummaryTokens(Number.POSITIVE_INFINITY)).toBe(DEFAULT_INJECTED_SUMMARY_TOKENS)
  })

  it('accepts a numeric string (a text field submits strings)', () => {
    expect(clampInjectedSummaryTokens('900')).toBe(900)
  })
})

describe('nearestInjectedSummaryPresetIndex', () => {
  it('returns the exact gear of an on-ladder budget', () => {
    INJECTED_SUMMARY_TOKEN_PRESETS.forEach((preset, index) => {
      expect(nearestInjectedSummaryPresetIndex(preset)).toBe(index)
    })
    expect(nearestInjectedSummaryPresetIndex(DEFAULT_INJECTED_SUMMARY_TOKENS))
      .toBe(INJECTED_SUMMARY_TOKEN_PRESETS.indexOf(DEFAULT_INJECTED_SUMMARY_TOKENS))
  })

  it('parks an off-ladder budget at the closest gear', () => {
    // 1200 is nearer 1500 than 800; 1000 is nearer 800.
    expect(nearestInjectedSummaryPresetIndex(1200)).toBe(2)
    expect(nearestInjectedSummaryPresetIndex(1000)).toBe(1)
    // Above the ladder it parks on the top gear, below it on the bottom one.
    expect(nearestInjectedSummaryPresetIndex(MAX_INJECTED_SUMMARY_TOKENS))
      .toBe(INJECTED_SUMMARY_TOKEN_PRESETS.length - 1)
    expect(nearestInjectedSummaryPresetIndex(0)).toBe(0)
  })

  it('falls back to the default gear for unusable values', () => {
    const defaultIndex = INJECTED_SUMMARY_TOKEN_PRESETS.indexOf(DEFAULT_INJECTED_SUMMARY_TOKENS)
    expect(nearestInjectedSummaryPresetIndex(Number.NaN)).toBe(defaultIndex)
    expect(nearestInjectedSummaryPresetIndex(undefined)).toBe(defaultIndex)
    expect(nearestInjectedSummaryPresetIndex('abc')).toBe(defaultIndex)
  })
})
