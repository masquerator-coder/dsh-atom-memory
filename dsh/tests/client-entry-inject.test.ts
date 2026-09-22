/**
 * Regression guard for the browser entry's `inject` declaration.
 *
 * WHY THIS TEST EXISTS: DSH 0.1.7-alpha.1 rewrote the client settings layer and
 * DELETED the `settingsScope` service. Cordis does not error on an unsatisfied
 * `inject` — the fiber parks in `pending` forever — and every unit test in this
 * repo still passed, because nothing here composes the real client graph. The
 * damage was visible only in a browser: the Web client's boot audit
 * (`packages/client/web/src/boot-client.ts` -> `assertEntriesActive`) requires
 * EVERY entry to be `active` and throws otherwise, which the boot page renders
 * as "Failed to load plugins" — the whole UI, not just this panel.
 *
 * So the stale name was invisible to `tsc` (the installed type stub still
 * declared it) and invisible to the suite. These assertions pin the two things
 * that actually have to hold at runtime:
 *   1. every service name in `inject` is one the client provides, and
 *   2. the entry resolves its settings form through `configForms`, not a binder.
 *
 * `dsh.client.inject` in package.json is a PACKAGE-name edge list used for
 * module arrival ordering, not a service list, so it is checked separately.
 *
 * The entry is read as TEXT rather than imported: `src/client/index.ts` imports
 * a `.tsx` component and is browser-only, so importing it here would drag the
 * whole client half into the node tsconfig project (which has no `--jsx`) and
 * break `npm run typecheck`. Parsing the source keeps this guard inside the
 * node half while still asserting on the real declaration. The built artifact
 * is checked too, since that is what the browser actually loads.
 */
import { readFileSync } from 'node:fs'
import { fileURLToPath } from 'node:url'
import { dirname, join } from 'node:path'
import { describe, expect, it } from 'vitest'

const here = dirname(fileURLToPath(import.meta.url))
const root = join(here, '..')

/** Read one repo file as UTF-8 text. */
function read(relative: string): string {
  return readFileSync(join(root, relative), 'utf8')
}

const pkg = JSON.parse(read('package.json')) as {
  dsh: { client: { inject: string[]; external: string[] } }
}

/**
 * Parse the `export const inject = [...]` array out of the browser entry.
 * @returns the declared service names, in order.
 */
function declaredInject(): string[] {
  const source = read('src/client/index.ts')
  const match = /export const inject = \[([^\]]*)\]/.exec(source)
  if (match === null) throw new Error('could not find the entry inject declaration')
  return [...match[1].matchAll(/'([^']+)'/g)].map(m => m[1])
}

/** Service names the DSH 0.1.7-alpha.1 client composition provides to plugins. */
const PROVIDED_SERVICES = new Set(['slots', 'locale', 'configForms', 'remote'])

describe('browser entry inject declaration', () => {
  it('declares the expected service list', () => {
    expect(declaredInject()).toEqual(['slots', 'locale', 'configForms', 'remote'])
  })

  it('never asks for the deleted settingsScope service', () => {
    expect(declaredInject()).not.toContain('settingsScope')
  })

  it('asks for configForms, the service that replaced settingsScope', () => {
    expect(declaredInject()).toContain('configForms')
  })

  it('lists only services the client composition actually provides', () => {
    // A name outside this set is exactly the failure mode above: cordis parks
    // the fiber in `pending` and the boot audit takes down the whole client.
    for (const service of declaredInject()) expect(PROVIDED_SERVICES).toContain(service)
  })

  it('resolves the settings form through configForms.get, not a binder', () => {
    const source = read('src/client/index.ts')
    expect(source).toContain('ctx.configForms.get')
    // The old API was `ctx.settingsScope.bind({ namespace: ... })`; a `.bind(`
    // left behind on configForms would be wrong too (get takes the namespace).
    // Comments legitimately narrate the old API, so only code lines count.
    const code = source
      .split('\n')
      .filter(line => !/^\s*(\*|\/\/|\/\*)/.test(line))
      .join('\n')
    expect(code).not.toMatch(/settingsScope\s*\.\s*bind/)
    expect(code).not.toMatch(/configForms\s*\.\s*bind/)
  })

  it('declares the ui-settings package edge so configForms arrives first', () => {
    // `dsh.client.inject` is package names, and configForms is provided by
    // ui-settings, so the row must be declared or the service is not yet there
    // when this entry's factory runs.
    expect(pkg.dsh.client.inject).toContain('@deepseek-ai/dsh-client-ui-settings')
  })

  it('leaves ui-settings external so configForms resolves through the module table', () => {
    // Bundling a private copy of ui-settings would give this plugin its own
    // ConfigForms service nobody provides for it.
    expect(pkg.dsh.client.external).toContain('@deepseek-ai/dsh-client-ui-settings')
  })

  describe('built browser artifact (what the client actually loads)', () => {
    /**
     * The shipped bundle, read as text. `lib/client.js` is the exact resource
     * the client-modules service serves at `exports["./client"]`, so a stale
     * build is indistinguishable from stale source in the browser.
     */
    const artifact = read('lib/client.js')
    /** Comment lines are documentation; only code may not name the dead service. */
    const artifactCode = artifact
      .split('\n')
      .filter(line => !line.trim().startsWith('*'))
      .join('\n')

    it('carries no functional reference to the deleted service', () => {
      expect(artifactCode).not.toContain('settingsScope')
    })

    it('carries the configForms inject entry and the get() call', () => {
      expect(artifactCode).toContain('"configForms"')
      expect(artifactCode).toContain('configForms.get')
    })
  })
})