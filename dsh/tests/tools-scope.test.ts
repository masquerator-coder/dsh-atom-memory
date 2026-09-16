/**
 * Tests for the scope-aware side of the memory surface.
 *
 * Two questions, both about what actually reaches the wire:
 *
 *  - does `scope_context` reach the right RPC params, from every read, write and
 *    automatic capture — and is it *absent* (not empty) when the deployment has
 *    no context, so a scope-blind setup keeps its exact previous behaviour;
 *  - does `memory_scope` do what its description says, and refuse clearly when
 *    it is called without the arguments an action needs.
 */
import { describe, it, expect, vi } from 'vitest'
import type { ToolDefinition } from '@deepseek-ai/dsh-tools'
import { registerMemoryTools, renderScopeResult } from '../src/tools.ts'
import { createCapture } from '../src/index.ts'
import type { ScopeContextPayload, SessionCwdSource } from '../src/scope.ts'

interface SetupOptions {
  extract?: (text: string) => Promise<unknown[]>
  /** The payload builder call sites get; absent means a scope-blind deployment. */
  scopeContext?: (source?: SessionCwdSource) => ScopeContextPayload | undefined
  isEnabled?: () => boolean
}

function setup(opts: SetupOptions = {}) {
  const bridge = { call: vi.fn() }
  const registered: ToolDefinition[] = []
  const tools = { register: (def: ToolDefinition) => { registered.push(def); return () => {} } }
  registerMemoryTools({
    ctx: { tools } as any,
    bridge: bridge as any,
    fallbackScope: 'global',
    maxRecalledFacts: 10,
    summaryTokens: 500,
    writeAckTimeoutMs: 2500,
    extract: opts.extract as any,
    scopeContext: opts.scopeContext,
    isEnabled: opts.isEnabled,
  })
  const tool = (name: string) => registered.find(d => d.name === name)!
  const render = (name: string, value: unknown, args: unknown = {}): string => {
    const out = tool(name).output as { render: (a: unknown, v: unknown) => Array<{ text: string }> }
    return out.render(args, value)[0]!.text
  }
  /** Arguments of RPC call `n` (default: the most recent), as the bridge sees them. */
  const callOf = (n = -1): [string, Record<string, unknown>] =>
    bridge.call.mock.calls.at(n) as unknown as [string, Record<string, unknown>]
  const lastParams = (): Record<string, unknown> => callOf()[1]
  const lastMethod = (): string => callOf()[0]
  return { bridge, registered, tool, render, callOf, lastParams, lastMethod }
}

const PAYLOAD: ScopeContextPayload = {
  signals: { git_root: '/repo', git_remote: 'git@github.com:owner/repo.git', path: '/repo' },
}

const execWithSession = (id = 's1', cwd?: string) =>
  ({ agent: { session: { id, header: cwd === undefined ? undefined : { cwd } } } }) as any

describe('scope_context reaches every scope-aware call', () => {
  it('rides along with a memory_add write (LLM-first path)', async () => {
    const scopeContext = vi.fn(() => PAYLOAD)
    const { bridge, tool, lastMethod, lastParams } = setup({
      extract: async () => [{ subject: '用户', predicate: '决定', object: 'x' }],
      scopeContext,
    })
    bridge.call.mockResolvedValue({ candidate_id: 'cand-1' })
    await tool('memory_add').execute({ content: '记住这件事' }, execWithSession('s1', '/repo'))
    expect(lastMethod()).toBe('persist_candidates')
    expect(lastParams().scope_context).toEqual(PAYLOAD)
    // The call site hands the run context over, so the builder can read that
    // session's working directory rather than the process's.
    expect(scopeContext).toHaveBeenCalledWith(expect.objectContaining({ agent: expect.anything() }))
  })

  it('rides along with the raw-knowledge fallback write', async () => {
    const longBody = '运维手册\n' + '部署前先备份数据库，再执行迁移脚本。'.repeat(10)
    const { bridge, tool, lastMethod, lastParams } = setup({
      extract: async () => [],
      scopeContext: () => PAYLOAD,
    })
    bridge.call.mockResolvedValue({ candidate_id: 'cand-raw' })
    await tool('memory_add').execute({ content: longBody }, execWithSession('s1', '/repo'))
    expect(lastMethod()).toBe('persist_candidates')
    expect(lastParams().scope_context).toEqual(PAYLOAD)
  })

  it('rides along with the rule-path write', async () => {
    const { tool, lastMethod, lastParams } = setup({
      extract: async () => [],
      scopeContext: () => PAYLOAD,
    })
    await tool('memory_add').execute({ content: '用户喜欢黑咖啡' }, execWithSession('s1', '/repo'))
    expect(lastMethod()).toBe('add')
    expect(lastParams().scope_context).toEqual(PAYLOAD)
  })

  it('rides along with memory_recall', async () => {
    const { bridge, tool, lastParams } = setup({ scopeContext: () => PAYLOAD })
    bridge.call.mockResolvedValue({ facts: [] })
    await tool('memory_recall').execute({ query: '部署' }, execWithSession('s1', '/repo'))
    expect(lastParams().scope_context).toEqual(PAYLOAD)
  })

  it('rides along with memory_summary and memory_summary_detail', async () => {
    const { bridge, tool, lastParams } = setup({ scopeContext: () => PAYLOAD })
    bridge.call.mockResolvedValue('digest')

    await tool('memory_summary').execute({}, execWithSession('s1', '/repo'))
    expect(lastParams()).toMatchObject({ detail: false })
    expect(lastParams().scope_context).toEqual(PAYLOAD)

    await tool('memory_summary_detail').execute({}, execWithSession('s1', '/repo'))
    expect(lastParams()).toMatchObject({ detail: true })
    // The detail depth ignores the context in the renderer; passing it anyway
    // keeps one rule instead of an exception to remember.
    expect(lastParams().scope_context).toEqual(PAYLOAD)
  })

  it('rides along with memory_replace', async () => {
    const { bridge, tool, lastParams } = setup({ scopeContext: () => PAYLOAD })
    bridge.call.mockResolvedValue({ status: 'applied' })
    await tool('memory_replace').execute({ factId: 'f1', content: '新内容' }, execWithSession('s1', '/repo'))
    expect(lastParams()).toMatchObject({ fact_id: 'f1' })
    expect(lastParams().scope_context).toEqual(PAYLOAD)
  })

  it('sends no scope_context key at all when there is no context to send', async () => {
    // Two ways to be scope-blind: no builder wired, and a builder that returns
    // nothing. Both must leave the params exactly as they were before scope
    // awareness existed — an empty object would be a different call.
    const blind = setup({ extract: async () => [] })
    await blind.tool('memory_add').execute({ content: '用户喜欢黑咖啡' }, execWithSession())
    expect('scope_context' in blind.lastParams()).toBe(false)

    const empty = setup({ extract: async () => [], scopeContext: () => undefined })
    await empty.tool('memory_add').execute({ content: '用户喜欢黑咖啡' }, execWithSession())
    expect('scope_context' in empty.lastParams()).toBe(false)

    const recall = setup({ scopeContext: () => undefined })
    recall.bridge.call.mockResolvedValue({ facts: [] })
    await recall.tool('memory_recall').execute({ query: 'x' }, execWithSession())
    expect('scope_context' in recall.lastParams()).toBe(false)

    const summary = setup({ scopeContext: () => undefined })
    summary.bridge.call.mockResolvedValue('')
    await summary.tool('memory_summary').execute({}, execWithSession())
    expect('scope_context' in summary.lastParams()).toBe(false)

    const replace = setup({ scopeContext: () => undefined })
    replace.bridge.call.mockResolvedValue({})
    await replace.tool('memory_replace').execute({ factId: 'f1', content: 'x' }, execWithSession())
    expect('scope_context' in replace.lastParams()).toBe(false)
  })
})

describe('the automatic capture path carries the same context', () => {
  const wiring = (opts: {
    extract?: (text: string) => Promise<unknown[]>
    scopeContextAt?: (cwd?: string) => ScopeContextPayload | undefined
    ready?: boolean
    enabled?: boolean
  } = {}) => {
    const call = vi.fn(async () => ({}))
    const scopeContextAt = vi.fn(opts.scopeContextAt ?? (() => PAYLOAD))
    const capture = createCapture({
      isEnabled: () => opts.enabled !== false,
      isReady: () => opts.ready !== false,
      extract: opts.extract as any,
      call,
      scopeContextAt,
    })
    /** Arguments of RPC call `n` (default: the most recent). */
    const callOf = (n = -1): [string, Record<string, unknown>] =>
      call.mock.calls.at(n) as unknown as [string, Record<string, unknown>]
    const lastParams = (): Record<string, unknown> => callOf()[1]
    const lastMethod = (): string => callOf()[0]
    return { call, capture, scopeContextAt, callOf, lastMethod, lastParams }
  }

  it('sends the session directory’s context with the LLM-first write', async () => {
    const { capture, scopeContextAt, lastMethod, lastParams } = wiring({
      extract: async () => [{ subject: '用户', predicate: '决定', object: 'x' }],
    })
    await capture('随便说的', 's1', '/repo')
    expect(lastMethod()).toBe('persist_candidates')
    expect(lastParams()).toMatchObject({ user_id: 'global', session_id: 's1' })
    expect(lastParams().scope_context).toEqual(PAYLOAD)
    expect(scopeContextAt).toHaveBeenCalledWith('/repo')
  })

  it('sends it with the rule-path write too', async () => {
    const { capture, lastMethod, lastParams } = wiring({ extract: async () => [] })
    await capture('用户喜欢黑咖啡', 's1', '/repo')
    expect(lastMethod()).toBe('add')
    expect(lastParams().scope_context).toEqual(PAYLOAD)
  })

  it('sends no scope_context key when the builder has nothing', async () => {
    const { capture, lastParams } = wiring({
      extract: async () => [{ subject: '用户', predicate: '决定', object: 'x' }],
      scopeContextAt: () => undefined,
    })
    await capture('随便说的', 's1')
    expect('scope_context' in lastParams()).toBe(false)
  })

  it('does not build a context, or call anything, while the memory switch is off', async () => {
    const { capture, call, scopeContextAt } = wiring({ enabled: false })
    await capture('随便说的', 's1', '/repo')
    expect(call).not.toHaveBeenCalled()
    expect(scopeContextAt).not.toHaveBeenCalled()
  })

  it('does not call the bridge while it is down', async () => {
    const { capture, call } = wiring({ ready: false })
    await capture('随便说的', 's1', '/repo')
    expect(call).not.toHaveBeenCalled()
  })
})

describe('memory_scope: list', () => {
  it('lists the tree, indented by path depth', async () => {
    const { bridge, tool, render, lastMethod, lastParams } = setup()
    bridge.call.mockResolvedValue([
      { scope_id: 1, scope_type: 'global', path: '/global', display_name: 'global', status: 'active', confidence: 1 },
      { scope_id: 3, scope_type: 'client', path: '/global/client:acme', display_name: 'acme', status: 'active', confidence: 0.9 },
      { scope_id: 4, scope_type: 'project', path: '/global/client:acme/project:api', display_name: 'api', status: 'active', confidence: 0.8 },
    ])
    const result = await tool('memory_scope').execute({ action: 'list' }, execWithSession())

    expect(lastMethod()).toBe('scope_list')
    expect(lastParams()).toEqual({})
    const text = render('memory_scope', result, { action: 'list' })
    expect(text).toContain('作用域树（3 个')
    expect(text).toContain('- [1] /global')
    expect(text).toContain('  - [3] /global/client:acme')
    expect(text).toContain('    - [4] /global/client:acme/project:api')
    expect(text).toContain('置信度 0.80')
  })

  it('passes a parent filter and a status through, and says so when empty', async () => {
    const { bridge, tool, render, lastParams } = setup()
    bridge.call.mockResolvedValue([])
    const result = await tool('memory_scope').execute(
      { action: 'list', parentId: 3, status: 'merged' },
      execWithSession(),
    )
    expect(lastParams()).toEqual({ parent_id: 3, status: 'merged' })
    expect(render('memory_scope', result, { action: 'list' })).toBe('（没有作用域）')
  })
})

describe('memory_scope: resolve', () => {
  it('reports the resolution and the unresolved candidate queue', async () => {
    const { bridge, tool, render, callOf, lastMethod, lastParams } = setup()
    bridge.call.mockImplementation(async (method: string) => {
      if (method === 'scope_resolve') {
        return {
          scope_id: 4, scope_type: 'project', path: '/global/client:acme/project:api',
          confidence: 0.75, status: 'auto', matched: ['git_remote'],
          conditions: [{ key: 'language', value: 'typescript' }],
          candidates: [], detail: 'matched git_remote',
        }
      }
      return [{ scope_type: 'project', name: 'work', signal_type: 'path', signal_value: 'd:/work', confidence: 0.5, seen_count: 3 }]
    })

    const result = await tool('memory_scope').execute({ action: 'resolve' }, execWithSession('s1', '/repo'))
    expect(lastMethod()).toBe('scope_unresolved')
    expect(callOf(0)[0]).toBe('scope_resolve')
    // Read-only: asking what the context is must not create a scope.
    expect(callOf(0)[1]).toMatchObject({ user_id: 'global', session_id: 's1', create: false })
    expect(lastParams()).toEqual({ user_id: 'global' })

    const text = render('memory_scope', result, { action: 'resolve' })
    expect(text).toContain('当前上下文 → [4] /global/client:acme/project:api（project）')
    expect(text).toContain('匹配信号：git_remote')
    expect(text).toContain('条件：language=typescript')
    expect(text).toContain('待确认候选（1 条）')
    expect(text).toContain('project "work" ← path: d:/work')
    expect(text).toContain('出现 3 次')
  })

  it('carries the current context, and distinguishes "global" from "in a scope"', async () => {
    const { bridge, tool, render, callOf } = setup({ scopeContext: () => PAYLOAD })
    bridge.call.mockImplementation(async (method: string) =>
      method === 'scope_resolve'
        ? { scope_id: 1, scope_type: 'global', path: '/global', confidence: 0.3, status: 'degraded' }
        : [])

    const result = await tool('memory_scope').execute({ action: 'resolve' }, execWithSession('s1', '/repo'))
    expect(callOf(0)[1].scope_context).toEqual(PAYLOAD)
    const text = render('memory_scope', result, { action: 'resolve' })
    expect(text).toContain('全局作用域')
    expect(text).toContain('待确认候选：无')
  })
})

describe('memory_scope: management actions', () => {
  it('creates a scope with its signals', async () => {
    const { bridge, tool, render, lastMethod, lastParams } = setup()
    bridge.call.mockResolvedValue({
      scope_id: 7, scope_type: 'project', path: '/global/project:api',
      display_name: 'api', status: 'active', confidence: 1,
    })
    const result = await tool('memory_scope').execute({
      action: 'create', scopeType: 'project', name: 'api',
      signals: { git_remote: 'git@github.com:owner/repo.git' },
    }, execWithSession())
    expect(lastMethod()).toBe('scope_create')
    expect(lastParams()).toEqual({
      scope_type: 'project',
      name: 'api',
      parent_id: undefined,
      signals: { git_remote: 'git@github.com:owner/repo.git' },
    })
    expect(render('memory_scope', result, { action: 'create' })).toContain('已创建（或已存在）作用域 [7] /global/project:api')
  })

  it('confirms a scope', async () => {
    const { bridge, tool, render, lastMethod, lastParams } = setup()
    bridge.call.mockResolvedValue({ scope_id: 4, confirmed: true })
    const result = await tool('memory_scope').execute({ action: 'confirm', scopeId: 4 }, execWithSession())
    expect(lastMethod()).toBe('scope_confirm')
    expect(lastParams()).toEqual({ scope_id: 4 })
    expect(render('memory_scope', result, { action: 'confirm' })).toContain('已确认作用域 [4]')
  })

  it('says when a confirmation did not take', async () => {
    const { bridge, tool, render } = setup()
    bridge.call.mockResolvedValue({ scope_id: 4, confirmed: false })
    const result = await tool('memory_scope').execute({ action: 'confirm', scopeId: 4 }, execWithSession())
    expect(render('memory_scope', result, { action: 'confirm' })).toContain('未确认')
  })

  it('adds an alias', async () => {
    const { bridge, tool, render, lastMethod, lastParams } = setup()
    bridge.call.mockResolvedValue({ scope_id: 4, alias: 'acme-api', alias_type: 'name', added: true })
    const result = await tool('memory_scope').execute(
      { action: 'alias_add', scopeId: 4, alias: 'acme-api' },
      execWithSession(),
    )
    expect(lastMethod()).toBe('scope_alias_add')
    expect(lastParams()).toEqual({ scope_id: 4, alias: 'acme-api' })
    expect(render('memory_scope', result, { action: 'alias_add' })).toContain('已为作用域 [4] 添加别名 "acme-api"')
  })

  it('reports an alias that already belongs to another scope', async () => {
    const { bridge, tool, render } = setup()
    bridge.call.mockResolvedValue({ scope_id: 4, alias: 'acme-api', alias_type: 'name', added: false })
    const result = await tool('memory_scope').execute(
      { action: 'alias_add', scopeId: 4, alias: 'acme-api' },
      execWithSession(),
    )
    expect(render('memory_scope', result, { action: 'alias_add' })).toContain('未添加')
  })

  it('merges two scopes and reports what moved', async () => {
    const { bridge, tool, render, lastMethod, lastParams } = setup()
    bridge.call.mockResolvedValue({
      from: 5, to: 4, facts_moved: 3, aliases_moved: 1, signals_moved: 2, children_moved: 0,
    })
    const result = await tool('memory_scope').execute({ action: 'merge', fromId: 5, toId: 4 }, execWithSession())
    expect(lastMethod()).toBe('scope_merge')
    expect(lastParams()).toEqual({ from_id: 5, to_id: 4 })
    const text = render('memory_scope', result, { action: 'merge' })
    expect(text).toContain('已把作用域 [5] 合并进 [4]')
    expect(text).toContain('事实绑定 3 条')
  })
})

describe('memory_scope: argument validation', () => {
  const cases: Array<[string, Record<string, unknown>, RegExp]> = [
    ['create without a type', { action: 'create', name: 'api' }, /scopeType/],
    ['create without a name', { action: 'create', scopeType: 'project' }, /name/],
    ['confirm without an id', { action: 'confirm' }, /scopeId/],
    ['alias_add without an id', { action: 'alias_add', alias: 'x' }, /scopeId/],
    ['alias_add without an alias', { action: 'alias_add', scopeId: 4 }, /alias/],
    ['merge without a source', { action: 'merge', toId: 4 }, /fromId/],
    ['merge without a target', { action: 'merge', fromId: 5 }, /toId/],
    ['an unknown action', { action: 'delete' }, /unknown action/],
  ]

  for (const [name, args, pattern] of cases) {
    it(`refuses ${name} without calling the store`, async () => {
      const { bridge, tool } = setup()
      await expect(tool('memory_scope').execute(args, execWithSession())).rejects.toThrow(pattern)
      expect(bridge.call).not.toHaveBeenCalled()
    })
  }

  it('refuses every action while the memory master switch is off', async () => {
    const { bridge, tool } = setup({ isEnabled: () => false })
    await expect(tool('memory_scope').execute({ action: 'list' }, execWithSession()))
      .rejects.toThrow(/disabled/)
    expect(bridge.call).not.toHaveBeenCalled()
  })
})

describe('memory_scope: render fallbacks', () => {
  it('falls back to the raw value for an action it does not know', () => {
    // `render` is also replayed over logged arguments, so an unrecognised
    // action must not render as an empty line.
    expect(renderScopeResult('reparent', { scope_id: 4 })).toBe('{"scope_id":4}')
  })

  it('renders a scope row defensively when fields are missing', () => {
    expect(renderScopeResult('create', {})).toContain('已创建（或已存在）作用域 [?] ?')
  })
})
