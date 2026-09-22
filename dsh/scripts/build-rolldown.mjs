/**
 * Fallback bundler entry for `tsdown.config.ts`.
 *
 * WHY THIS EXISTS: `tsdown` (via `rolldown-plugin-dts`) statically imports
 * `yuku-parser`, whose prebuilt `yuku-parser.node` is blocked on this machine by
 * a Windows Application Control policy ("An Application Control policy has
 * blocked this file"). tsdown therefore cannot even resolve its own options
 * here, and it wipes `outDir` before failing — leaving `lib/` empty and the dsh
 * Web client serving nothing.
 *
 * This script drives `rolldown` directly — the same bundler tsdown wraps — using
 * the two configs from `tsdown.config.ts`, so the emitted artifacts are what
 * tsdown would have produced. It is a local build escape hatch, not a
 * replacement: `npm run build` stays the supported path.
 *
 * Usage: node scripts/build-rolldown.mjs
 */
import { readFileSync } from 'node:fs'
import { spawnSync } from 'node:child_process'
import { createRequire } from 'node:module'
import { dirname, join } from 'node:path'
import { fileURLToPath } from 'node:url'

const here = dirname(fileURLToPath(import.meta.url))
const root = join(here, '..')

/**
 * Resolve a pnpm-isolated package by walking the `.pnpm` store, because strict
 * pnpm layouts do not expose transitive deps (`rolldown`) to the root.
 * @param spec - `name` or `name@version`, scoped names allowed.
 * @returns the absolute package directory.
 */
function resolvePnpm(spec) {
  const store = join(root, 'node_modules', '.pnpm')
  const entries = readFileSync(join(root, 'node_modules', '.modules.yaml'), 'utf8')
  if (!entries.includes('nodeLinker: hoisted')) { /* layout independent; store still keyed by name */ }
  const { readdirSync } = require('node:fs')
  const [name, version] = spec.startsWith('@')
    ? [spec.split('@').slice(0, 2).join('@'), spec.split('@')[2]]
    : spec.split('@')
  const dirName = name.replace('/', '+')
  const match = readdirSync(store).find(entry =>
    entry === dirName || entry.startsWith(`${dirName}@`) &&
    (version === undefined || entry.startsWith(`${dirName}@${version}`)))
  if (match === undefined) throw new Error(`build-rolldown: ${spec} not found under .pnpm`)
  return join(store, match, 'node_modules', name)
}

const require = createRequire(import.meta.url)
const { rolldown } = require(resolvePnpm('rolldown@1.2.8'))

/**
 * Emit `lib/index.d.mts` with `tsc`, standing in for tsdown's `dts: true`.
 *
 * `tsc` writes `index.d.ts`; the manifest's `types` field points at
 * `index.d.mts`, so the file is renamed to match what tsdown would have emitted
 * for an `.mts`-declared ESM entry. The project tsconfig is reused verbatim
 * (`allowImportingTsExtensions`, `rewriteRelativeImportExtensions`, ...) rather
 * than passing a hand-rolled flag list, which would not match the source's
 * `.ts`-suffixed imports. Output goes to a temp dir so the intermediate
 * per-module declarations never land in `lib/`.
 * @returns true when the declaration bundle was emitted.
 */
function runTscDts() {
  const { mkdtempSync, copyFileSync, existsSync, rmSync } = require('node:fs')
  const { tmpdir } = require('node:os')
  const tsc = resolvePnpm('typescript@5.9.3')
  const staging = mkdtempSync(join(tmpdir(), 'atom-memory-dts-'))
  const result = spawnSync(process.execPath, [
    join(tsc, 'bin', 'tsc'),
    '-p', 'tsconfig.build-dts.json',
    '--outDir', staging,
  ], { cwd: root, stdio: 'inherit' })
  const emitted = join(staging, 'index.d.ts')
  if (result.status !== 0 || !existsSync(emitted)) {
    rmSync(staging, { recursive: true, force: true })
    return false
  }
  // Copy, not rename: the temp dir may live on another volume (EXDEV).
  copyFileSync(emitted, join(root, 'lib', 'index.d.mts'))
  rmSync(staging, { recursive: true, force: true })
  console.log('build-rolldown: wrote lib/index.d.mts')
  return true
}

// Mirrors PLATFORM_MODULES + CLIENT_EXTERNALS in tsdown.config.ts.
const CLIENT_EXTERNALS = new Set([
  'react', 'react/jsx-runtime', 'react-dom', 'react-dom/client', '@deepseek-ai/cordis',
  '@deepseek-ai/dsh-client-store',
  '@deepseek-ai/dsh-client-ui-slots',
  '@deepseek-ai/dsh-client-ui-primitives',
  '@deepseek-ai/dsh-client-ui-dockkit',
  '@deepseek-ai/dsh-client-locale',
  '@deepseek-ai/dsh-client-ui-settings',
  '@deepseek-ai/dsh-client-ui-renderer',
  '@deepseek-ai/dsh-api-remotes',
])

// tsdown's `deps.alwaysBundle` default: bundle anything not marked external.
const alwaysBundle = {
  name: 'dsh-atom-memory-always-bundle',
  resolveId(source) {
    if (CLIENT_EXTERNALS.has(source)) return { id: source, external: true }
    return null
  },
}

/**
 * Run one rolldown config and write its output.
 * @param options - rolldown input options plus the output file name.
 */
async function build({ input, outFile, banner, footer, intro }) {
  const bundle = await rolldown({
    input,
    platform: 'browser',
    resolve: { extensions: ['.ts', '.tsx', '.js', '.jsx', '.json'] },
    plugins: [alwaysBundle],
    // Mirrors tsdown's `define` block. rolldown nests it under `transform`;
    // putting it at the top level is silently ignored (and warns), which would
    // leave `process.env.NODE_ENV` unsubstituted in any bundled dependency.
    transform: {
      typescript: { jsx: 'react-jsx' },
      define: {
        'process.env.NODE_ENV': JSON.stringify(process.env.NODE_ENV ?? 'production'),
        'import.meta.env.MODE': JSON.stringify(process.env.NODE_ENV ?? 'production'),
        'import.meta.env': JSON.stringify({ MODE: process.env.NODE_ENV ?? 'production' }),
      },
    },
  })
  await bundle.write({
    file: outFile,
    format: 'cjs',
    banner,
    footer,
    intro,
    sourcemap: true,
    exports: 'named',
  })
  await bundle.close()
}

const outDir = join(root, 'lib')

// --- Face 1: the node host half (lib/index.mjs) ---------------------------
// Same shape tsdown emits for `src/index.ts`: plain ESM for the dsh Loader.
// Everything is bundled (the node half's deps are host-provided or pure), and
// `lib/index.d.mts` is generated separately below.
{
  const bundle = await rolldown({
    input: join(root, 'src/index.ts'),
    platform: 'node',
    resolve: { extensions: ['.ts', '.tsx', '.js', '.jsx', '.json'] },
    transform: { typescript: { jsx: 'react-jsx' } },
  })
  await bundle.write({
    file: join(outDir, 'index.mjs'),
    format: 'esm',
    sourcemap: false,
  })
  await bundle.close()
  console.log('build-rolldown: wrote lib/index.mjs')
}

// --- Face 2: the browser client bundle (lib/client.js) --------------------
// The harness's module-table format: every module-table row stays a
// `require()` the injected table answers, everything else is inlined.
await build({
  input: join(root, 'src/client/index.ts'),
  outFile: join(outDir, 'client.js'),
  banner: 'window.__ModuleLoader__.load({ id: "dsh-atom-memory", factory: (require) => {',
  footer: 'return module.exports; } });',
  intro: 'var module = { exports: {} }; var exports = module.exports;',
})
console.log('build-rolldown: wrote lib/client.js')

// --- Face 3: the emitted type declarations --------------------------------
// `tsdown`'s `dts: true` produces this via rolldown-plugin-dts, which is the
// component that cannot load here. `tsc` emits declaration-only output instead,
// which needs no native parser.
if (!runTscDts()) {
  console.error('build-rolldown: declaration emit failed')
  process.exit(1)
}