/**
 * Browser-half Remote contribution for the `atomMemory` namespace.
 *
 * The Host side (`../controller.ts`) exposes the memory panel's data operations
 * as `@Remote` methods under the `atomMemory` wire namespace. The browser has no
 * auto-generated `ctx.remote.atomMemory` — the harness only mounts the
 * contributions it explicitly selects. This module hands that same namespace's
 * descriptors to `ctx.remote.$mount(...)`, so the browser-side
 * `ctx.remote.atomMemory.<method>` stubs exist and call through the Gateway to
 * the Host `AtomMemoryController`.
 *
 * Descriptors here are mirror-images of the Host SRC claims: every method takes
 * a single JSON `args` payload and returns a JSON business value, so a shared
 * pass-through strict codec suffices (the wire fully round-trips plain JSON).
 *
 * @module dsh-atom-memory/client/remote
 */
import type {
  InvocationDescriptor,
  TypertCodec,
  TypertRemoteContribution,
  TypertSchema,
} from '@deepseek-ai/dsh-typert-protocol'

/** The Remote wire namespace this browser half mounts (matches the Host binding). */
export const REMOTE_NAMESPACE = 'atomMemory'

/**
 * The pass-through boundary schema for this namespace's plain-JSON values.
 *
 * The Host SRC claim for the same endpoints uses `{ mode: 'src-json' }`, so both
 * ends already agree the wire value is plain JSON that needs no structural
 * narrowing: this schema only has to exist, never to narrow anything.
 */
const JSON_SCHEMA: TypertSchema<unknown> = { parse: (value: unknown) => value }

/**
 * One strict boundary codec carrying BOTH keys of the `TypertCodec` contract
 * change that landed after the 0.1.6-alpha.1 release (dsh commit e459e32637,
 * "perf(typert): materialize generated schemas on first use"):
 *
 *  - a harness built from a source past that commit validates descriptors at
 *    mount time and decodes through `create().parse(...)`, rejecting an eager
 *    `schema` codec with "typert: <ns>/<method> result strict codec has no
 *    create() factory" — thrown inside `ctx.remote.$mount(...)`, so this
 *    browser half's `apply()` fails and the ENTIRE web boot stops on
 *    "Failed to load plugins" while the Host half keeps working;
 *  - the last npm-published build (0.1.6-alpha.1, and every 0.1.5-rc.x before
 *    it) reads `schema.parse` and knows nothing about `create()`.
 *
 * Carrying both keys mounts on either build. The published typings lag the new
 * key too, so the object is assembled before being returned as `TypertCodec`:
 * a direct object literal would trip excess-property checking on `create`.
 *
 * @param typeSymbol - canonical wire type symbol for the boundary value.
 * @returns the strict codec for both halves of this contribution.
 */
function strictJsonCodec(typeSymbol: string): TypertCodec {
  const codec = {
    mode: 'strict' as const,
    typeSymbol,
    create: () => JSON_SCHEMA,
    schema: JSON_SCHEMA,
  }
  return codec
}

const JSON_CODEC: TypertCodec = strictJsonCodec('dsh-atom-memory#JsonValue')

/** One descriptor for a Host method whose single argument is a JSON `args` object. */
function jsonArgsMethod(method: string, hasArgs: boolean): InvocationDescriptor {
  return {
    id: `${REMOTE_NAMESPACE}/${method}`,
    service: 'atomMemoryController',
    namespace: REMOTE_NAMESPACE,
    method,
    invocation: { kind: 'direct' },
    parameters: hasArgs
      ? [{ name: 'args', wire: 'args', source: 'json', codec: JSON_CODEC }]
      : [],
    result: JSON_CODEC,
  }
}

/** The `atomMemory` contribution mounted by this browser half. */
export const ATOM_MEMORY_REMOTE: TypertRemoteContribution = {
  package: 'dsh-atom-memory',
  descriptors: [
    jsonArgsMethod('listFacts', true),
    jsonArgsMethod('editFact', true),
    jsonArgsMethod('deleteFact', true),
    jsonArgsMethod('summary', true),
    jsonArgsMethod('listProfile', true),
    jsonArgsMethod('upsertProfile', true),
    jsonArgsMethod('deleteProfile', true),
    jsonArgsMethod('backup', true),
    jsonArgsMethod('restore', true),
    jsonArgsMethod('getRuntime', false),
  ],
}
