/**
 * Tests for scope-aware context collection.
 *
 * The collector reads a filesystem, so every case here runs on a fake one: the
 * suite has no git checkout to walk, and the layouts that matter (a worktree's
 * `.git` *file*, a credential in a remote, an unreadable config) are exactly the
 * ones a real repository would not conveniently provide.
 *
 * Two properties are asserted throughout, because they are what makes the
 * feature safe to enable by default: collection never throws, and a context with
 * nothing in it is not sent at all.
 */
import { describe, it, expect, beforeEach, vi } from 'vitest'
import {
  buildScopeContext,
  clearScopeSignalCache,
  collectScopeSignals,
  parseRemoteOriginUrl,
  scopeContextForCwd,
  sessionCwdOf,
  stripCredentials,
  type ScopeFs,
} from '../src/scope.ts'

/** Parent directory of a `/`-separated path, or `undefined` at the root. */
function dirnameOf(path: string): string | undefined {
  const trimmed = path.replace(/\/+$/u, '')
  const idx = trimmed.lastIndexOf('/')
  if (idx < 0) return undefined
  return idx === 0 ? '/' : trimmed.slice(0, idx)
}

interface FakeFsSpec {
  /** File path -> contents. */
  files?: Record<string, string>
  /** Directories that exist without holding a modelled file. */
  dirs?: string[]
  /** Every `readText` call, in order (cache assertions). */
  reads?: string[]
}

/**
 * Build a fake filesystem from a file/directory listing.
 *
 * Deliberately total: `exists`/`isDirectory` answer from the listing without
 * touching a real disk, so a case can describe "a `.git` file pointing at a
 * worktree that is not there" as easily as a normal clone.
 */
function fakeFs(spec: FakeFsSpec = {}, opts: { allThrows?: boolean; readThrows?: boolean } = {}): ScopeFs {
  const files = new Map<string, string>(Object.entries(spec.files ?? {}))
  const dirs = new Set<string>(spec.dirs ?? [])
  for (const file of files.keys()) {
    let dir = dirnameOf(file)
    while (dir !== undefined) {
      dirs.add(dir)
      dir = dirnameOf(dir)
    }
  }
  // Annotated as a never-returning function so the calls below are recognised
  // as throws and the code after them keeps its narrowed type.
  const boom: () => never = () => { throw new Error('EACCES') }
  return {
    exists: (path) => (opts.allThrows === true ? boom() : files.has(path) || dirs.has(path)),
    isDirectory: (path) => (opts.allThrows === true ? boom() : dirs.has(path) && !files.has(path)),
    readText: (path) => {
      spec.reads?.push(path)
      if (opts.readThrows === true || opts.allThrows === true) boom()
      const body = files.get(path)
      if (body === undefined) boom()
      return body
    },
    join: (...parts) => parts.join('/').replace(/\/{2,}/gu, '/'),
    parent: (path) => dirnameOf(path),
  }
}

const GIT_CONFIG = [
  '[core]',
  '\trepositoryformatversion = 0',
  '\tbare = false',
  '[remote "origin"]',
  '\turl = git@github.com:owner/repo.git',
  '\tfetch = +refs/heads/*:refs/remotes/origin/*',
  '',
].join('\n')

beforeEach(() => {
  clearScopeSignalCache()
})

describe('collectScopeSignals', () => {
  it('reads the working directory, its git root, remote and declared package', () => {
    const fs = fakeFs({
      files: {
        '/repo/.git/config': GIT_CONFIG,
        '/repo/package.json': '{"name":"my-package","version":"1.0.0"}',
      },
    })
    const signals = collectScopeSignals({ cwd: '/repo/src', fs })
    expect(signals).toEqual({
      path: '/repo/src',
      git_root: '/repo',
      git_remote: 'git@github.com:owner/repo.git',
      package: 'my-package',
    })
  })

  it('walks up to the repository root from a nested directory', () => {
    const fs = fakeFs({
      files: { '/repo/.git/config': GIT_CONFIG, '/repo/packages/app/package.json': '{"name":"app"}' },
    })
    const signals = collectScopeSignals({ cwd: '/repo/packages/app', fs })
    expect(signals.git_root).toBe('/repo')
    // The repository root declares no manifest (a monorepo), so the one the
    // session actually sits in names the project.
    expect(signals.package).toBe('app')
  })

  it('prefers the repository manifest over a nested one', () => {
    const fs = fakeFs({
      files: {
        '/repo/.git/config': GIT_CONFIG,
        '/repo/package.json': '{"name":"root-pkg"}',
        '/repo/packages/app/package.json': '{"name":"app"}',
      },
    })
    expect(collectScopeSignals({ cwd: '/repo/packages/app', fs }).package).toBe('root-pkg')
  })

  it('reads a pyproject.toml [project] name when there is no package.json', () => {
    const fs = fakeFs({
      files: {
        '/repo/.git/config': GIT_CONFIG,
        '/repo/pyproject.toml': '[build-system]\nrequires = ["setuptools"]\n\n[project]\nname = "my-package"\nversion = "0.1.0"\n',
      },
    })
    expect(collectScopeSignals({ cwd: '/repo', fs }).package).toBe('my-package')
  })

  it('ignores a pyproject.toml name that is not in [project]', () => {
    const fs = fakeFs({
      files: { '/repo/pyproject.toml': '[tool.poetry]\nname = "legacy"\n' },
    })
    // A legacy poetry manifest is a known gap; inventing a package name from an
    // unrelated table would be a wrong identity, which is worse than none.
    expect(collectScopeSignals({ cwd: '/repo', fs }).package).toBeUndefined()
  })

  it('follows a worktree .git file to the git dir that holds the config', () => {
    const fs = fakeFs({
      files: {
        '/wt/.git': 'gitdir: /main/.git/worktrees/wt\n',
        '/main/.git/worktrees/wt/config': GIT_CONFIG,
      },
    })
    const signals = collectScopeSignals({ cwd: '/wt/src', fs })
    expect(signals.git_root).toBe('/wt')
    expect(signals.git_remote).toBe('git@github.com:owner/repo.git')
  })

  it('resolves a relative worktree gitdir against the .git file’s directory', () => {
    const fs = fakeFs({
      files: {
        '/work/wt/.git': 'gitdir: ../main/.git/worktrees/wt\n',
        '/work/main/.git/worktrees/wt/config': GIT_CONFIG,
      },
    })
    const signals = collectScopeSignals({ cwd: '/work/wt', fs })
    expect(signals.git_root).toBe('/work/wt')
    expect(signals.git_remote).toBe('git@github.com:owner/repo.git')
  })

  it('still reports the git root when the worktree gitdir cannot be read', () => {
    const fs = fakeFs({ files: { '/wt/.git': 'gitdir: /gone/worktrees/wt\n' } })
    const signals = collectScopeSignals({ cwd: '/wt', fs })
    // A `.git` file is a repository boundary even when its target is missing:
    // continuing the walk would attach the session to an unrelated parent repo.
    expect(signals.git_root).toBe('/wt')
    expect(signals.git_remote).toBeUndefined()
  })

  it('emits only the path when there is no repository or manifest in sight', () => {
    const fs = fakeFs({ dirs: ['/tmp/scratch'] })
    expect(collectScopeSignals({ cwd: '/tmp/scratch', fs })).toEqual({ path: '/tmp/scratch' })
  })

  it('never throws: an unreadable filesystem just means fewer signals', () => {
    const fs = fakeFs({
      files: { '/repo/.git/config': GIT_CONFIG, '/repo/package.json': '{"name":"p"}' },
    }, { readThrows: true })
    const signals = collectScopeSignals({ cwd: '/repo', fs })
    expect(signals).toEqual({ path: '/repo', git_root: '/repo' })
  })

  it('never throws even when probing the filesystem itself fails', () => {
    const fs = fakeFs({ files: { '/repo/.git/config': GIT_CONFIG } }, { allThrows: true })
    expect(collectScopeSignals({ cwd: '/repo', fs })).toEqual({})
  })

  it('returns nothing for a blank working directory', () => {
    expect(collectScopeSignals({ cwd: '   ', fs: fakeFs() })).toEqual({})
  })

  it('does not throw when the process directory itself is gone', () => {
    // `process.cwd()` throws for a deleted directory; that is "no signals", not
    // a failure on the capture path.
    const cwd = vi.spyOn(process, 'cwd').mockImplementation(() => { throw new Error('ENOENT') })
    try {
      expect(collectScopeSignals({ fs: fakeFs() })).toEqual({})
    } finally {
      cwd.mockRestore()
    }
  })

  it('caches one directory’s facts and hands out a copy of them', () => {
    const reads: string[] = []
    const fs = fakeFs({
      files: { '/repo/.git/config': GIT_CONFIG, '/repo/package.json': '{"name":"p"}' },
      reads,
    })
    const first = collectScopeSignals({ cwd: '/repo', fs })
    const readsAfterFirst = reads.length
    expect(readsAfterFirst).toBeGreaterThan(0)

    // A caller mutating what it was handed must not poison the next call site.
    first.git_remote = 'tampered'
    const second = collectScopeSignals({ cwd: '/repo', fs })
    expect(second.git_remote).toBe('git@github.com:owner/repo.git')
    expect(reads.length).toBe(readsAfterFirst)
  })

  it('re-reads after the cache is cleared, and when asked to bypass it', () => {
    const reads: string[] = []
    const fs = fakeFs({ files: { '/repo/.git/config': GIT_CONFIG }, reads })
    collectScopeSignals({ cwd: '/repo', fs })
    const readsAfterFirst = reads.length
    expect(readsAfterFirst).toBeGreaterThan(0)

    collectScopeSignals({ cwd: '/repo', fs, fresh: true })
    expect(reads.length).toBeGreaterThan(readsAfterFirst)

    clearScopeSignalCache()
    const readsBeforeThird = reads.length
    collectScopeSignals({ cwd: '/repo', fs })
    expect(reads.length).toBeGreaterThan(readsBeforeThird)
  })

  it('keeps the cache bounded: an old directory must be re-read eventually', () => {
    const reads: string[] = []
    const fs = fakeFs({ files: { '/d0/package.json': '{"name":"p"}' }, reads })
    collectScopeSignals({ cwd: '/d0', fs })
    const readsForFirst = reads.filter(r => r.startsWith('/d0/')).length

    // Whatever the bound is, it exists to stop an unbounded map growing in a
    // long-lived process; 40 distinct directories exceed any sane bound.
    for (let i = 1; i < 40; i += 1) collectScopeSignals({ cwd: `/d${i}`, fs })

    const before = reads.filter(r => r.startsWith('/d0/')).length
    collectScopeSignals({ cwd: '/d0', fs })
    expect(reads.filter(r => r.startsWith('/d0/')).length).toBeGreaterThan(before)
    expect(readsForFirst).toBeGreaterThan(0)
  })
})

describe('parseRemoteOriginUrl', () => {
  it('reads only the origin section', () => {
    const config = [
      '[remote "upstream"]',
      '\turl = git@github.com:other/fork.git',
      '[remote "origin"]',
      '\turl = https://example.com/team/repo.git',
      '',
    ].join('\n')
    expect(parseRemoteOriginUrl(config)).toBe('https://example.com/team/repo.git')
  })

  it('ignores keys outside the origin section and comment lines', () => {
    const config = [
      '# url = git@github.com:comment/ignored.git',
      '[core]',
      '\turl = not-a-remote',
      '[remote "origin"]',
      '\t; url = git@github.com:commented/ignored.git',
      '',
    ].join('\n')
    expect(parseRemoteOriginUrl(config)).toBeUndefined()
  })

  it('accepts a quoted url and an origin with no url key', () => {
    expect(parseRemoteOriginUrl('[remote "origin"]\n\turl = "git@github.com:o/r.git"\n'))
      .toBe('git@github.com:o/r.git')
    expect(parseRemoteOriginUrl('[remote "origin"]\n\tfetch = +refs/heads/*\n')).toBeUndefined()
  })

  it('returns undefined for a config that is not a config', () => {
    expect(parseRemoteOriginUrl('gitdir: /main/.git/worktrees/wt')).toBeUndefined()
    expect(parseRemoteOriginUrl('')).toBeUndefined()
  })
})

describe('stripCredentials', () => {
  it('drops an embedded user and token from a URL-form remote', () => {
    expect(stripCredentials('https://user:ghp_secret@github.com/owner/repo.git'))
      .toBe('https://github.com/owner/repo.git')
    expect(stripCredentials('https://ghp_secret@github.com/owner/repo.git'))
      .toBe('https://github.com/owner/repo.git')
  })

  it('drops the userinfo but keeps host, port and path', () => {
    expect(stripCredentials('ssh://git@example.com:2222/team/repo.git'))
      .toBe('ssh://example.com:2222/team/repo.git')
  })

  it('keeps the scp-like form: it carries a user name, not a credential', () => {
    expect(stripCredentials('git@github.com:owner/repo.git')).toBe('git@github.com:owner/repo.git')
  })

  it('leaves a URL without userinfo alone', () => {
    expect(stripCredentials('https://github.com/owner/repo.git')).toBe('https://github.com/owner/repo.git')
    expect(stripCredentials('file:///srv/git/repo.git')).toBe('file:///srv/git/repo.git')
  })
})

describe('buildScopeContext', () => {
  it('returns undefined when there is genuinely nothing to send', () => {
    // Not an empty object: a scope-blind deployment's params must stay exactly
    // what they were before scope awareness existed.
    expect(buildScopeContext()).toBeUndefined()
    expect(buildScopeContext({ signals: {}, conditions: {}, phase: '  ', scopeHint: '' }))
      .toBeUndefined()
  })

  it('sends collected signals under their own types', () => {
    expect(buildScopeContext({ signals: { path: '/repo', git_root: '/repo' } }))
      .toEqual({ signals: { path: '/repo', git_root: '/repo' } })
  })

  it('adds explicit tags as their own signal types and as the phase', () => {
    const payload = buildScopeContext({
      explicit: { org: 'acme', client: 'acme', project: 'api', series: 'news', phase: 'draft' },
    })
    expect(payload).toEqual({
      signals: {
        explicit_org: 'acme',
        explicit_client: 'acme',
        explicit_project: 'api',
        explicit_series: 'news',
        explicit_phase: 'draft',
      },
      phase: 'draft',
    })
  })

  it('never lets an explicit tag overwrite an observed signal of the same type', () => {
    const payload = buildScopeContext({
      signals: { explicit_project: 'observed' },
      explicit: { project: 'tagged' },
    })
    // "The user said so" and "the path looks like it" are different evidence
    // with different weights; only the tag is emitted here, and the observed
    // value keeps its place under its own type.
    expect(payload?.signals?.explicit_project).toBe('tagged')
  })

  it('drops blank values instead of sending empty evidence', () => {
    const payload = buildScopeContext({
      signals: { path: '/repo', git_remote: '   ' },
      explicit: { org: '', client: '   ' },
      conditions: { language: 'typescript', doc_type: '' },
      scopeHint: '   ',
    })
    expect(payload).toEqual({
      signals: { path: '/repo' },
      conditions: { language: 'typescript' },
    })
  })

  it('carries conditions, an explicit phase and the extractor hint', () => {
    const payload = buildScopeContext({
      conditions: { doc_type: 'proposal' },
      phase: 'review',
      scopeHint: 'acme onboarding deck',
    })
    expect(payload).toEqual({
      conditions: { doc_type: 'proposal' },
      phase: 'review',
      scope_hint: 'acme onboarding deck',
    })
  })
})

describe('scopeContextForCwd', () => {
  it('collects the directory and applies the explicit tags', () => {
    const fs = fakeFs({ files: { '/repo/.git/config': GIT_CONFIG } })
    const payload = scopeContextForCwd('/repo', { explicit: { client: 'acme' }, fs })
    expect(payload).toEqual({
      signals: {
        path: '/repo',
        git_root: '/repo',
        git_remote: 'git@github.com:owner/repo.git',
        explicit_client: 'acme',
      },
    })
  })

  it('still sends the explicit tags when the directory yields only its path', () => {
    const payload = scopeContextForCwd(undefined, { explicit: { project: 'api' }, fs: fakeFs() })
    expect(payload?.signals).toMatchObject({ explicit_project: 'api' })
  })

  it('falls back to the process directory when no cwd is known', () => {
    const payload = scopeContextForCwd(undefined, { fs: fakeFs({ dirs: [process.cwd()] }) })
    expect(payload?.signals?.path.length).toBeGreaterThan(0)
  })
})

describe('sessionCwdOf', () => {
  it('reads the session header of a tool run or assembly context', () => {
    expect(sessionCwdOf({ agent: { session: { header: { cwd: '/repo' } } } })).toBe('/repo')
  })

  it('reads a session passed directly (what the capture hooks hold)', () => {
    expect(sessionCwdOf({ header: { cwd: '/repo/src' } })).toBe('/repo/src')
  })

  it('is undefined without a session, a header or a usable value', () => {
    expect(sessionCwdOf(undefined)).toBeUndefined()
    expect(sessionCwdOf({})).toBeUndefined()
    expect(sessionCwdOf({ agent: { session: {} } })).toBeUndefined()
    expect(sessionCwdOf({ agent: { session: { header: { cwd: '   ' } } } })).toBeUndefined()
  })
})
