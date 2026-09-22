/**
 * Build dispatcher for this plugin's two faces.
 *
 * `npm run build` stays the supported entry point; this script only decides HOW
 * to run it. The preferred path is `tsdown` (the harness's own bundler). Some
 * machines cannot run it at all: tsdown statically imports
 * `rolldown-plugin-dts/internal`, which loads the prebuilt `yuku-parser.node`
 * at module-init time, and a Windows Application Control policy can block that
 * binary ("An Application Control policy has blocked this file"). In that case
 * tsdown dies while resolving its options — AFTER cleaning `outDir`, which
 * leaves `lib/` empty and makes the dsh Web client serve nothing at all.
 *
 * So this wrapper:
 *   1. probes whether the native parser can load, and
 *   2. either runs `tsdown && transpile-decorators`, or falls back to
 *      `scripts/build-rolldown.mjs` (the same bundler tsdown wraps) and then
 *      runs the decorator pass, and
 *   3. verifies `lib/` actually contains both halves, so a silent partial build
 *      can never be mistaken for success.
 *
 * Why `lib/` must be non-empty is not cosmetic: `exports["./client"]` is served
 * verbatim to the browser (the client-modules service reads the file; it never
 * compiles TS), so a missing or stale artifact is exactly what ships.
 */
import { spawnSync } from 'node:child_process'
import { existsSync } from 'node:fs'
import { createRequire } from 'node:module'
import { dirname, join } from 'node:path'
import { fileURLToPath } from 'node:url'

const here = dirname(fileURLToPath(import.meta.url))
const root = join(here, '..')

/**
 * Run a command, inheriting stdio, and return whether it succeeded.
 *
 * No `shell: true`: `process.execPath` on Windows is typically
 * `C:\Program Files\nodejs\node.exe`, and a shell splits it at the space.
 * @param command - executable to run.
 * @param args - arguments passed verbatim.
 * @returns true on exit status 0.
 */
function run(command, args) {
  const result = spawnSync(command, args, { cwd: root, stdio: 'inherit' })
  return result.status === 0
}

/**
 * Whether tsdown's native parser dependency is loadable at all.
 * @returns true when `yuku-parser` can be imported.
 */
function parserAvailable() {
  const require = createRequire(import.meta.url)
  try {
    // Resolve through tsdown's own dependency edge, which is where the binding
    // is reachable from (pnpm does not hoist it to this package root).
    const tsdownPkg = require.resolve('tsdown/package.json', { paths: [root] })
    const from = createRequire(tsdownPkg)
    from('yuku-parser')
    return true
  } catch {
    return false
  }
}

const useTsdown = parserAvailable()
console.log(useTsdown
  ? 'build: using tsdown'
  : 'build: tsdown unavailable (its native parser binding is blocked on this machine); using the rolldown fallback')

let bundled
if (useTsdown) {
  // Invoke tsdown through its own CLI entry rather than `npx`, so no shell is
  // involved (see `run`).
  const require = createRequire(import.meta.url)
  const tsdownPkg = require.resolve('tsdown/package.json', { paths: [root] })
  const tsdownBin = join(dirname(tsdownPkg), 'dist', 'cli.mjs')
  bundled = run(process.execPath, [tsdownBin])
} else {
  bundled = run(process.execPath, [join(here, 'build-rolldown.mjs')])
}

if (!bundled) {
  console.error('build: bundling failed')
  process.exit(1)
}

if (!run(process.execPath, [join(here, 'transpile-decorators.mjs')])) {
  console.error('build: decorator transpile pass failed')
  process.exit(1)
}

// Both faces must exist: the node half the Loader imports, and the browser
// bundle the client-modules service serves.
const required = ['lib/index.mjs', 'lib/client.js']
const missing = required.filter(relative => !existsSync(join(root, relative)))
if (missing.length > 0) {
  console.error(`build: missing expected output: ${missing.join(', ')}`)
  process.exit(1)
}
console.log(`build: ok (${required.join(', ')})`)