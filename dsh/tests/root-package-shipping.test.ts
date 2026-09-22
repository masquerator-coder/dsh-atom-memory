/**
 * Regression guard for the ROOT bundle shell's packaging and client edges.
 *
 * WHY THIS TEST EXISTS: the repo installs as one dsh profile layer through the
 * root `package.json` (a "bundle shell" whose `main`/`exports` point into
 * `dsh/lib`). Two of its fields are load-bearing at runtime and neither is
 * reachable from `tsc` or from any browser-side unit test:
 *
 *   1. `files` decides what `dsh plugin add <git-url>` actually copies. The
 *      built Node half opens with
 *      `createRequire(import.meta.url)("../package.json")` to read its own
 *      version — a path that resolves to `dsh/package.json`. That inner
 *      manifest is NOT in the Loader's resolution path from the root, so when
 *      `files` omits it the file is stripped on install and the very first
 *      line of `dsh/lib/index.mjs` throws
 *      `Cannot find module '../package.json'`.
 *
 *      The symptom is badly misleading: dsh reports
 *      `atom-memory (dsh-atom-memory): failed to import` and the settings
 *      section simply never appears — looking exactly like a client/render bug
 *      while the fault is a missing file in the published artifact. Every test
 *      in this repo still passed, because the suite runs against the working
 *      tree where `dsh/package.json` is present.
 *
 *   2. `dsh.client.inject` is the browser module-arrival edge list. The entry
 *      calls `ctx.slots.register`, so `ui-slots` must be declared or the
 *      `slots` service is not yet provided when the entry's factory runs —
 *      the fiber parks in `pending` and the boot audit rejects the client.
 *      `dsh/package.json` carries the same declaration, but the ROOT manifest
 *      is the one the profile reads, so the two can silently drift.
 *
 * These assertions read the two manifests as text and require them to agree on
 * the client edges, and require `files` to ship every path the built artifact
 * resolves at import time.
 */
import { existsSync, readFileSync } from 'node:fs'
import { fileURLToPath } from 'node:url'
import { dirname, join, posix } from 'node:path'
import { describe, expect, it } from 'vitest'

const here = dirname(fileURLToPath(import.meta.url))
const repoRoot = join(here, '..', '..')

/** One package manifest, narrowed to the fields this guard reasons about. */
interface Manifest {
  main?: string
  exports?: Record<string, unknown>
  files?: string[]
  dsh?: {
    bundle?: { patch?: string }
    client?: { inject?: string[]; external?: string[]; platform?: string }
  }
}

/** Read and parse one manifest, relative to the repo root. */
function manifest(relative: string): Manifest {
  return JSON.parse(readFileSync(join(repoRoot, relative), 'utf8')) as Manifest
}

const rootPkg = manifest('package.json')
const innerPkg = manifest('dsh/package.json')

/** The `exports["./*"]` target string, or undefined when it is not a plain string. */
function exportTarget(pkg: Manifest, key: string): string | undefined {
  const entry = pkg.exports?.[key]
  if (typeof entry === 'string') return entry
  if (entry !== null && typeof entry === 'object' && 'default' in entry) {
    const value = (entry as { default?: unknown }).default
    if (typeof value === 'string') return value
  }
  return undefined
}

describe('root bundle shell packaging', () => {
  it('points main and exports at the built Node half', () => {
    // The profile mounts the ROOT package, so these are the paths the Loader
    // imports. A root `main` that misses `dsh/lib` makes the entry unimportable.
    expect(rootPkg.main).toBe('./dsh/lib/index.mjs')
    expect(exportTarget(rootPkg, '.')).toBe('./dsh/lib/index.mjs')
    expect(exportTarget(rootPkg, './client')).toBe('./dsh/lib/client.js')
  })

  it('ships every file the built Node half resolves at import time', () => {
    // `dsh/lib/index.mjs` reads its version through
    // `createRequire(import.meta.url)("../package.json")`, which resolves to
    // `dsh/package.json`. Omitting it from `files` strips it on install and the
    // module throws before exporting anything.
    const required = 'dsh/package.json'
    expect(existsSync(join(repoRoot, required))).toBe(true)
    expect(rootPkg.files ?? []).toContain(required)
  })

  it('ships the manifests, config and client sources the profile reads', () => {
    for (const entry of ['dsh/lib', 'dsh/src/client', 'dsh/cordis.patch.yml']) {
      expect(rootPkg.files ?? []).toContain(entry)
    }
  })

  it('ships the patch the bundle layer declares', () => {
    // `dsh.bundle.patch` names a file the profile loads; shipping the field
    // without the file makes the layer unresolvable.
    const patch = rootPkg.dsh?.bundle?.patch
    expect(patch).toBeTruthy()
    // The patch path is written relative to the package root; `files` entries
    // are too. npm manifest paths are always POSIX, so normalize in that form
    // (a `node:path` normalize would emit backslashes on Windows and never
    // match a `files` entry).
    const relative = posix.normalize(patch!.replace(/^\.\//, ''))
    expect(rootPkg.files ?? []).toContain(relative)
    expect(existsSync(join(repoRoot, relative))).toBe(true)
  })
})

describe('root client edges agree with the inner manifest', () => {
  it('declares the same dsh.client.inject list in both manifests', () => {
    // The two manifests describe the same browser bundle. The ROOT one is what
    // the profile reads; a drift here is invisible until the panel disappears.
    expect(rootPkg.dsh?.client?.inject).toEqual(innerPkg.dsh?.client?.inject)
  })

  it('declares the same dsh.client.external list in both manifests', () => {
    expect(rootPkg.dsh?.client?.external).toEqual(innerPkg.dsh?.client?.external)
  })

  it('declares the same client platform in both manifests', () => {
    expect(rootPkg.dsh?.client?.platform).toBe(innerPkg.dsh?.client?.platform)
    expect(rootPkg.dsh?.client?.platform).toBe('web')
  })

  it('declares ui-slots in the root inject list', () => {
    // The entry calls `ctx.slots.register`; without this edge the `slots`
    // service is absent when the factory runs and the fiber parks in `pending`.
    expect(rootPkg.dsh?.client?.inject).toContain('@deepseek-ai/dsh-client-ui-slots')
  })

  it('leaves ui-slots external instead of bundling a private copy', () => {
    expect(rootPkg.dsh?.client?.external).toContain('@deepseek-ai/dsh-client-ui-slots')
  })
})