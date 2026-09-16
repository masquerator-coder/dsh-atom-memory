/**
 * Scope-aware context collection.
 *
 * The Python store can place memory in a hierarchy (org / client / project /
 * phase / document / thread), but it cannot observe *where* a session is
 * working: it only ever sees text. This module is the half of that job the host
 * owns — read the few environment facts that identify a session's location
 * (its working directory, the git root and origin remote above it, the package
 * it declares) and turn them, together with the deployment's explicit tags,
 * into the `scope_context` payload every scope-aware RPC call carries.
 *
 * Three properties shape the code:
 *
 *  - **Collecting never breaks a memory call.** Every read is best-effort: an
 *    unreadable file, a permission error or a bogus `.git` marker simply means
 *    fewer signals, never a thrown error on the capture path (which runs on
 *    every user message).
 *  - **Nothing is sent when there is nothing to say.** {@link buildScopeContext}
 *    returns `undefined` rather than an empty object, so a deployment with no
 *    usable signal — or one that switched `scopeEnabled` off — sends exactly the
 *    RPC params it sent before scope awareness existed.
 *  - **A remote URL is an identity, not a secret.** A git remote routinely
 *    embeds a token (`https://user:token@host/…`); credentials are stripped
 *    before the value becomes a signal, and the raw config text is never logged.
 *
 * @module dsh-atom-memory/scope
 */
import { existsSync, readFileSync, statSync } from 'node:fs'
import { dirname, join } from 'node:path'

/** Signal types this module can emit (the rest of the contract is other hosts'). */
const SIGNAL_PATH = 'path'
const SIGNAL_GIT_ROOT = 'git_root'
const SIGNAL_GIT_REMOTE = 'git_remote'
const SIGNAL_PACKAGE = 'package'

/** Explicit-tag signal types, in the wire naming the Python side recognises. */
const EXPLICIT_ORG = 'explicit_org'
const EXPLICIT_CLIENT = 'explicit_client'
const EXPLICIT_PROJECT = 'explicit_project'
const EXPLICIT_SERIES = 'explicit_series'
const EXPLICIT_PHASE = 'explicit_phase'

/**
 * Upper bound on cached working directories.
 *
 * Small on purpose: one plugin instance sees the handful of directories a user
 * works in, and an unbounded map in a long-lived process is a leak with no
 * upside (a miss only costs a few `stat` calls).
 */
const SIGNAL_CACHE_MAX = 32

/** Longest remote URL kept as a signal; anything longer is not a remote. */
const MAX_REMOTE_CHARS = 2048

/**
 * The filesystem surface the collector reads through.
 *
 * Injected for two reasons: the test suite never sits in a real git checkout
 * (so the interesting layouts — a worktree's `.git` *file*, an unreadable
 * config, a missing manifest — have to be expressible), and a deployment's
 * filesystem quirks must be testable without mocking a module.
 */
export interface ScopeFs {
  /** Whether a path exists at all. */
  exists(path: string): boolean
  /** Whether a path is an existing directory (false for a file or a miss). */
  isDirectory(path: string): boolean
  /**
   * Read a UTF-8 text file.
   *
   * @throws when the file cannot be read — callers treat that as "no signal".
   */
  readText(path: string): string
  /** Join path segments with the platform separator. */
  join(...parts: string[]): string
  /** The parent directory, or `undefined` at a filesystem root. */
  parent(path: string): string | undefined
}

/** The real filesystem. Every probe is total: a throwing syscall reads as "no". */
const nodeFs: ScopeFs = {
  exists: (path) => {
    try {
      return existsSync(path)
    } catch {
      return false
    }
  },
  isDirectory: (path) => {
    try {
      return statSync(path).isDirectory()
    } catch {
      return false
    }
  },
  readText: (path) => readFileSync(path, 'utf8'),
  join: (...parts) => join(...parts),
  parent: (path) => {
    const up = dirname(path)
    // `dirname` of a root returns the root itself; that sentinel is what stops
    // the walk-up loop.
    return up === path ? undefined : up
  },
}

/** Free-text explicit tags from the composition config. */
export interface ScopeExplicitTags {
  org?: string
  client?: string
  project?: string
  series?: string
  phase?: string
}

/**
 * The `scope_context` wire payload.
 *
 * Field names are the contract's verbatim (snake_case): this object is
 * serialized straight into the RPC params, so a renamed key would be silently
 * ignored by the Python side rather than reported.
 */
export interface ScopeContextPayload {
  /** `signal_type` -> raw value. */
  signals?: Record<string, string>
  /** Conditions of the current context, e.g. `{doc_type: 'proposal'}`. */
  conditions?: Record<string, string>
  /** Current phase (also a scope type of its own). */
  phase?: string
  /** Free-text hint about where the work belongs; the weakest evidence. */
  scope_hint?: string
}

/**
 * Anything that carries a session's working directory.
 *
 * Structural rather than a concrete harness type, because the call sites hold
 * differently-shaped objects: a tool run and a prompt assembly reach the session
 * through the agent, while the capture hooks hold the session itself. The only
 * thing this module reads is the session header's `cwd`.
 */
export interface SessionCwdSource {
  agent?: { session?: { header?: { cwd?: string } } } | undefined
  /** Present when the object *is* the session rather than an agent-bearing context. */
  header?: { cwd?: string } | undefined
}

/** Signals collected for one working directory, in wire naming. */
type Signals = Record<string, string>

/**
 * Cached filesystem facts, keyed by working directory, oldest insertion first.
 *
 * Why caching is safe: the facts collected for a directory — where its git root
 * is, what its origin remote is called, what its manifest is named — do not
 * change while a session runs, and every call site (each capture, each tool
 * call, the session's prompt freeze) asks for the same directory. Re-reading
 * them would put a handful of syscalls on the path of every user message for an
 * answer that cannot have changed. The map is bounded, so a process that visits
 * many directories evicts the oldest instead of growing.
 */
const signalCache = new Map<string, Signals>()

/**
 * Forget every cached working directory.
 *
 * Exists for tests (a fake filesystem is mutated between cases) and for an
 * operator-facing reset; nothing in the plugin calls it in normal operation.
 */
export function clearScopeSignalCache(): void {
  signalCache.clear()
}

/**
 * Read the working directory a session reports.
 *
 * @param source - A tool run, an assembly context, or a session itself.
 * @returns The trimmed cwd, or `undefined` when the caller has no session.
 */
export function sessionCwdOf(source: SessionCwdSource | undefined): string | undefined {
  const cwd = source?.agent?.session?.header?.cwd ?? source?.header?.cwd
  if (typeof cwd !== 'string') return undefined
  const trimmed = cwd.trim()
  return trimmed.length > 0 ? trimmed : undefined
}

/** Everything the collector reads, so a test can supply its own world. */
export interface CollectScopeSignalsOptions {
  /**
   * Working directory to observe. Absent means the plugin's own process cwd,
   * which is the right answer for a single-project harness and the only one
   * available when a call site carries no session header.
   */
  cwd?: string
  /** Filesystem surface; defaults to `node:fs`. */
  fs?: ScopeFs
  /** Bypass the per-directory cache (tests, and a caller that just wrote a file). */
  fresh?: boolean
}

/**
 * Collect the environment signals for one working directory.
 *
 * Never throws: a failed read is an absent signal, and a directory that yields
 * nothing beyond its own path still yields `path` (reliability 0.50, which
 * needs corroboration before it can create a scope — see the Python side's
 * reliability table).
 *
 * @param opts - Working directory and injectable filesystem.
 * @returns `signal_type` -> raw value, with credentials stripped from any remote.
 */
export function collectScopeSignals(opts: CollectScopeSignalsOptions = {}): Signals {
  const fs = opts.fs ?? nodeFs
  let cwd: string
  try {
    // `process.cwd()` itself throws when the directory it would return no longer
    // exists (a deleted checkout), which is one more way "no signals" must not
    // become "a thrown error on the capture path".
    cwd = (opts.cwd ?? process.cwd()).trim()
  } catch {
    return {}
  }
  if (cwd.length === 0) return {}

  if (opts.fresh !== true) {
    const cached = signalCache.get(cwd)
    // Copied out, so a caller cannot mutate what the next call site reads.
    if (cached !== undefined) return { ...cached }
  }

  let signals: Signals
  try {
    signals = readSignals(fs, cwd)
  } catch {
    // Documented guarantee: collection is best-effort, so an unexpected
    // filesystem failure degrades to "no signals" instead of throwing into the
    // capture path.
    signals = {}
  }

  if (opts.fresh !== true) {
    signalCache.delete(cwd)
    signalCache.set(cwd, signals)
    while (signalCache.size > SIGNAL_CACHE_MAX) {
      const oldest = signalCache.keys().next().value
      if (oldest === undefined) break
      signalCache.delete(oldest)
    }
  }
  return { ...signals }
}

/**
 * Collect a working directory's signals and assemble the wire payload.
 *
 * The one entry point the plugin's call sites use: it keeps the "which
 * directory" question in one place and makes the scope-blind case explicit.
 *
 * @param cwd - The session's working directory, when it has one.
 * @param opts - Explicit tags plus injectable filesystem (tests).
 * @returns The payload, or `undefined` when nothing is worth sending.
 */
export function scopeContextForCwd(
  cwd: string | undefined,
  opts: {
    explicit?: ScopeExplicitTags
    fs?: ScopeFs
    fresh?: boolean
  } = {},
): ScopeContextPayload | undefined {
  return buildScopeContext({
    signals: collectScopeSignals({ cwd, fs: opts.fs, fresh: opts.fresh }),
    explicit: opts.explicit,
  })
}

/**
 * Inputs of {@link buildScopeContext}.
 *
 * The dsh plugin supplies only `signals` and `explicit`: a coding session knows
 * its directory, not the document type it is writing. The other three are the
 * rest of the wire contract, assembled here rather than at each call site so a
 * host that *does* know them (a document tool, a mail connector) can send them
 * without a second payload builder — and so the "nothing to send" rule covers
 * them too.
 */
export interface BuildScopeContextOptions {
  /** Signals from {@link collectScopeSignals} (or any other collector). */
  signals?: Record<string, string>
  /** Free-text explicit tags from the config; the strongest evidence there is. */
  explicit?: ScopeExplicitTags
  /** Conditions under which the current work is happening. */
  conditions?: Record<string, string>
  /** Current phase; falls back to the explicit `phase` tag. */
  phase?: string
  /** Free-text location hint from the extractor. */
  scopeHint?: string
}

/**
 * Assemble the `scope_context` payload.
 *
 * Returns `undefined` — not an empty object — when there is genuinely nothing
 * to send: signals, conditions, phase and hint all empty. That is what keeps a
 * scope-blind deployment's RPC params byte-identical to what they were before
 * scope awareness existed, instead of every call carrying `scope_context: {}`.
 *
 * @param opts - Signals plus explicit tags, conditions, phase and hint.
 * @returns The payload, or `undefined` when it would carry no evidence.
 */
export function buildScopeContext(opts: BuildScopeContextOptions = {}): ScopeContextPayload | undefined {
  const signals: Signals = {}
  const put = (key: string, value: string | undefined): void => {
    const trimmed = (value ?? '').trim()
    if (trimmed.length > 0) signals[key] = trimmed
  }

  for (const [key, value] of Object.entries(opts.signals ?? {})) put(key, value)
  // Explicit tags are emitted under their own signal types rather than
  // overwriting an observed one: "the user said this is project X" and "the cwd
  // looks like project X" are different evidence, and the store weights them
  // differently (0.95 vs 0.50-0.90).
  put(EXPLICIT_ORG, opts.explicit?.org)
  put(EXPLICIT_CLIENT, opts.explicit?.client)
  put(EXPLICIT_PROJECT, opts.explicit?.project)
  put(EXPLICIT_SERIES, opts.explicit?.series)
  put(EXPLICIT_PHASE, opts.explicit?.phase)

  const conditions: Record<string, string> = {}
  for (const [key, value] of Object.entries(opts.conditions ?? {})) {
    const cleanKey = key.trim()
    const cleanValue = (value ?? '').trim()
    if (cleanKey.length > 0 && cleanValue.length > 0) conditions[cleanKey] = cleanValue
  }

  const phase = (opts.phase ?? opts.explicit?.phase ?? '').trim()
  const hint = (opts.scopeHint ?? '').trim()

  const payload: ScopeContextPayload = {}
  if (Object.keys(signals).length > 0) payload.signals = signals
  if (Object.keys(conditions).length > 0) payload.conditions = conditions
  if (phase.length > 0) payload.phase = phase
  if (hint.length > 0) payload.scope_hint = hint
  return Object.keys(payload).length === 0 ? undefined : payload
}

/**
 * Strip credentials from a remote URL before it becomes a signal.
 *
 * A remote is frequently written with a token embedded
 * (`https://user:token@host/owner/repo.git`, `https://token@host/…`). The
 * identity that matters is `host/owner/repo`, and a stored signal is a value
 * the store will keep and later render, so the userinfo is dropped. The
 * scp-like form (`git@github.com:owner/repo.git`) is returned verbatim: it
 * carries a *user name*, not a credential, and rewriting it would only make the
 * value differ from what the user sees in their own config.
 *
 * @param url - The raw remote URL from the git config.
 * @returns The URL without userinfo; the input when it has none.
 */
export function stripCredentials(url: string): string {
  const text = (url ?? '').trim()
  const scheme = /^[a-zA-Z][a-zA-Z0-9+.-]*:\/\//u.exec(text)
  if (scheme === null) return text
  const rest = text.slice(scheme[0].length)
  const authorityEnd = rest.search(/[/?#]/u)
  const authority = authorityEnd === -1 ? rest : rest.slice(0, authorityEnd)
  const tail = authorityEnd === -1 ? '' : rest.slice(authorityEnd)
  const at = authority.lastIndexOf('@')
  if (at === -1) return text
  return `${scheme[0]}${authority.slice(at + 1)}${tail}`
}

/**
 * Read `remote.origin.url` out of a git config file.
 *
 * Only the `[remote "origin"]` section is read, and only its `url` key: a
 * hand-rolled scan of a few lines beats a dependency, and the alternative
 * (matching the first `url =` anywhere) would happily return a *different*
 * remote's URL — a wrong identity is worse than no identity, because it would
 * bind facts to the wrong project silently.
 *
 * @param configText - The config file's contents.
 * @returns The raw URL, or `undefined` when the repository has no origin.
 */
export function parseRemoteOriginUrl(configText: string): string | undefined {
  let inOriginSection = false
  for (const rawLine of (configText ?? '').split(/\r?\n/u)) {
    const line = rawLine.trim()
    if (line.length === 0 || line.startsWith('#') || line.startsWith(';')) continue
    const section = /^\[(.+)\]$/u.exec(line)
    if (section !== null) {
      inOriginSection = /^remote\s+"origin"$/iu.test(section[1]!.trim())
      continue
    }
    if (!inOriginSection) continue
    const url = /^url\s*=\s*(.*)$/iu.exec(line)
    if (url !== null) {
      const value = url[1]!.trim().replace(/^"(.*)"$/u, '$1')
      if (value.length > 0) return value
    }
  }
  return undefined
}

/** A git working tree found by walking up from a directory. */
interface GitLocation {
  /** The working-tree root (the directory that holds the `.git` marker). */
  root: string
  /** The git directory: `.git` itself, or the target of a worktree `.git` file. */
  gitDir: string
}

/** Read a file, treating any failure as "not there". */
function tryRead(fs: ScopeFs, path: string): string | undefined {
  try {
    if (!fs.exists(path)) return undefined
    return fs.readText(path)
  } catch {
    return undefined
  }
}

/**
 * Find the git working tree containing `start`.
 *
 * `.git` may be a directory (an ordinary clone) or a *file* naming the git dir
 * (a linked worktree, a submodule): both mark a repository boundary, and a file
 * whose target cannot be read still marks one, so the walk-up stops there
 * rather than continuing into an unrelated parent checkout.
 *
 * @param fs - Filesystem surface.
 * @param start - Directory to start from.
 * @returns The location, or `undefined` when no ancestor is a working tree.
 */
function findGitRoot(fs: ScopeFs, start: string): GitLocation | undefined {
  const visited = new Set<string>()
  let dir: string | undefined = start
  while (dir !== undefined && !visited.has(dir)) {
    visited.add(dir)
    const marker = fs.join(dir, '.git')
    if (fs.exists(marker)) {
      if (fs.isDirectory(marker)) return { root: dir, gitDir: marker }
      const target = tryRead(fs, marker)
      const gitDir = target === undefined ? undefined : gitDirTarget(fs, dir, target)
      return { root: dir, gitDir: gitDir ?? marker }
    }
    dir = fs.parent(dir)
  }
  return undefined
}

/**
 * Resolve the path a worktree's `.git` file points at (`gitdir: <path>`).
 *
 * The target is usually relative to the directory holding the `.git` file, so
 * it needs `..` handling; only `join`/`parent` are used, which keeps the
 * filesystem surface small and works on both separator conventions.
 *
 * @param fs - Filesystem surface.
 * @param base - Directory containing the `.git` file.
 * @param text - The `.git` file's contents.
 * @returns An absolute-ish path, or `undefined` when the file has no target.
 */
function gitDirTarget(fs: ScopeFs, base: string, text: string): string | undefined {
  const match = /^\s*gitdir\s*:\s*(.+?)\s*$/imu.exec(text)
  if (match === null) return undefined
  const target = match[1]!.replace(/\\/gu, '/')
  const absolute = target.startsWith('/') || /^[a-zA-Z]:\//u.test(target)
  let current = absolute ? '' : base
  for (const part of target.split('/')) {
    if (part === '' || part === '.') continue
    if (part === '..') {
      current = fs.parent(current) ?? current
      continue
    }
    if (current === '') {
      // A root segment: `/x` on POSIX, `C:` on Windows (where joining an empty
      // base with the drive would produce a relative path).
      current = /^[a-zA-Z]:$/u.test(part) ? part : `/${part}`
      continue
    }
    current = fs.join(current, part)
  }
  return current === '' ? undefined : current
}

/**
 * Read `remote.origin.url` for a working tree.
 *
 * Both candidate locations are tried: the git dir's `config` (the normal case,
 * and the only one a linked worktree has) and `<root>/.git/config` (which is
 * the same file for an ordinary clone, and simply unreadable when `.git` is a
 * file). The first file that yields an origin wins.
 *
 * @param fs - Filesystem surface.
 * @param repo - The located working tree.
 * @returns The credential-stripped remote URL, or `undefined`.
 */
function readRemoteOrigin(fs: ScopeFs, repo: GitLocation): string | undefined {
  const candidates = [fs.join(repo.gitDir, 'config'), fs.join(repo.root, '.git', 'config')]
  for (const file of candidates) {
    const text = tryRead(fs, file)
    if (text === undefined) continue
    const url = parseRemoteOriginUrl(text)
    if (url === undefined) continue
    const stripped = stripCredentials(url)
    if (stripped.length > 0 && stripped.length <= MAX_REMOTE_CHARS) return stripped
  }
  return undefined
}

/** Read the declared package name out of a `package.json` body. */
function packageNameFromJson(text: string | undefined): string | undefined {
  if (text === undefined) return undefined
  try {
    const parsed = JSON.parse(text) as { name?: unknown }
    const name = parsed?.name
    if (typeof name !== 'string') return undefined
    const trimmed = name.trim()
    return trimmed.length > 0 ? trimmed : undefined
  } catch {
    return undefined
  }
}

/**
 * Read the declared package name out of a `pyproject.toml` body.
 *
 * Only the PEP 621 `[project]` table is read (`name = "…"`), which is what a
 * modern project declares; a legacy `[tool.poetry]`-only manifest is a known
 * gap, and the signal is optional by design (its reliability is 0.55, below
 * every auto-bind threshold on its own).
 *
 * @param text - The manifest body, when it could be read.
 * @returns The package name, or `undefined`.
 */
function packageNameFromToml(text: string | undefined): string | undefined {
  if (text === undefined) return undefined
  let inProjectTable = false
  for (const rawLine of text.split(/\r?\n/u)) {
    const line = rawLine.trim()
    const section = /^\[(.+)\]$/u.exec(line)
    if (section !== null) {
      inProjectTable = section[1]!.trim() === 'project'
      continue
    }
    if (!inProjectTable) continue
    const name = /^name\s*=\s*["']([^"']+)["']/u.exec(line)
    if (name !== null) {
      const trimmed = name[1]!.trim()
      if (trimmed.length > 0) return trimmed
    }
  }
  return undefined
}

/**
 * Read the declared package name, looking in each directory in turn.
 *
 * The git root is tried before the working directory: the repository is the
 * durable identity the store can match across machines, while a nested
 * directory's manifest is only read when the root declares none (a monorepo
 * whose root has no `package.json`).
 *
 * @param fs - Filesystem surface.
 * @param dirs - Candidate directories, in priority order.
 * @returns The package name, or `undefined`.
 */
function readPackageName(fs: ScopeFs, dirs: Array<string | undefined>): string | undefined {
  const seen = new Set<string>()
  for (const dir of dirs) {
    if (dir === undefined || seen.has(dir)) continue
    seen.add(dir)
    const fromJson = packageNameFromJson(tryRead(fs, fs.join(dir, 'package.json')))
    if (fromJson !== undefined) return fromJson
    const fromToml = packageNameFromToml(tryRead(fs, fs.join(dir, 'pyproject.toml')))
    if (fromToml !== undefined) return fromToml
  }
  return undefined
}

/**
 * Read every signal available for one working directory.
 *
 * @param fs - Filesystem surface.
 * @param cwd - Working directory (already trimmed and known non-empty).
 * @returns The signals found; always at least `path`.
 */
function readSignals(fs: ScopeFs, cwd: string): Signals {
  const signals: Signals = { [SIGNAL_PATH]: cwd }
  const repo = findGitRoot(fs, cwd)
  if (repo !== undefined) {
    signals[SIGNAL_GIT_ROOT] = repo.root
    const remote = readRemoteOrigin(fs, repo)
    if (remote !== undefined) signals[SIGNAL_GIT_REMOTE] = remote
  }
  const pkg = readPackageName(fs, [repo?.root, cwd])
  if (pkg !== undefined) signals[SIGNAL_PACKAGE] = pkg
  return signals
}
