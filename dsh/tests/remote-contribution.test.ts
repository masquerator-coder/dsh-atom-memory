/**
 * Unit tests for the browser Remote contribution (`src/client/remote.ts`).
 *
 * Verifies the `atomMemory` descriptors mount correctly for the Gateway client:
 * every method is `direct`, carries only strict JSON codecs (the client mount
 * rejects plain `src-json` codecs), and exposes the exact method set the Host
 * `AtomMemoryController` marks with `@Remote`.
 */
import { describe, expect, it } from 'vitest'
import {
  ATOM_MEMORY_REMOTE,
  REMOTE_NAMESPACE,
} from '../src/client/remote.ts'

/**
 * The two-sided strict-codec contract the descriptors must satisfy (see
 * `strictJsonCodec` in `src/client/remote.ts`): a harness built past dsh commit
 * e459e32637 validates `create()` and decodes through `create().parse(...)`,
 * while the last npm-published build reads `schema.parse`. The published typings
 * know only the latter, so read both keys through one widening cast rather than
 * teaching TypeScript a contract its package types cannot see.
 */
interface RuntimeStrictCodec {
  readonly mode: 'strict'
  readonly typeSymbol: string
  readonly create: () => { parse(value: unknown): unknown }
  readonly schema: { parse(value: unknown): unknown }
}

const asRuntimeStrict = (codec: unknown): RuntimeStrictCodec => codec as RuntimeStrictCodec

describe('ATOM_MEMORY_REMOTE', () => {
  it('targets the atomMemory namespace with all Host-exposed methods', () => {
    expect(REMOTE_NAMESPACE).toBe('atomMemory')
    expect(ATOM_MEMORY_REMOTE.package).toBe('dsh-atom-memory')
    const methods = ATOM_MEMORY_REMOTE.descriptors.map(d => d.method)
    expect(methods).toEqual([
      'listFacts',
      'editFact',
      'deleteFact',
      'summary',
      'listProfile',
      'upsertProfile',
      'deleteProfile',
      'backup',
      'restore',
      'getRuntime',
    ])
    for (const descriptor of ATOM_MEMORY_REMOTE.descriptors) {
      expect(descriptor.namespace).toBe('atomMemory')
    }
  })

  it('uses only strict JSON codecs so the Gateway client accepts the mount', () => {
    for (const descriptor of ATOM_MEMORY_REMOTE.descriptors) {
      expect(descriptor.invocation).toEqual({ kind: 'direct' })
      expect(descriptor.result.mode).toBe('strict')
      if (descriptor.result.mode === 'strict') {
        const result = asRuntimeStrict(descriptor.result)
        // Both keys, because each side of the upstream contract change has to
        // find its own: `create()` for a current harness build, `schema` for
        // the last npm-published one (dropping either one fails the mount, and
        // a failed browser-half mount aborts the whole web boot).
        expect(typeof result.create).toBe('function')
        expect(typeof result.create().parse).toBe('function')
        expect(typeof result.schema.parse).toBe('function')
      }
      for (const parameter of descriptor.parameters) {
        expect(parameter.source).toBe('json')
        const codec = asRuntimeStrict(parameter.codec)
        expect(codec.mode).toBe('strict')
        // Strict-plus-JSON: parse is a pass-through, never a narrowing loss.
        expect(typeof codec.create().parse).toBe('function')
        expect(typeof codec.schema.parse).toBe('function')
      }
    }
  })

  it('gives every arg-carrying method a single named wire field for `args`', () => {
    for (const descriptor of ATOM_MEMORY_REMOTE.descriptors) {
      if (descriptor.method === 'getRuntime') {
        expect(descriptor.parameters).toHaveLength(0)
        continue
      }
      expect(descriptor.parameters).toHaveLength(1)
      expect(descriptor.parameters[0]?.name).toBe('args')
      expect(descriptor.parameters[0]?.wire).toBe('args')
    }
  })

  it('matches the Host @Remote marker set exactly', async () => {
    // Independent source-of-truth: parse the Host controller source for @Remote.
    const source = await import('node:fs/promises').then(m =>
      m.readFile(new URL('../src/controller.ts', import.meta.url), 'utf8'))
    const marked: string[] = []
    for (const match of source.matchAll(/@Remote[\s\S]*?\n\s*(?:async\s+)?([A-Za-z_$][\w$]*)\s*\(/g)) {
      marked.push(match[1])
    }
    expect([...ATOM_MEMORY_REMOTE.descriptors].map(d => d.method).sort())
      .toEqual([...marked].sort())
  })
})
