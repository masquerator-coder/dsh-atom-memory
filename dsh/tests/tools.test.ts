import { describe, it, expect, vi } from 'vitest'
import type { ToolDefinition } from '@deepseek-ai/dsh-tools'
import { registerMemoryTools, renderStats, renderOverviewStatus } from '../src/tools.ts'

/**
 * Regression test for the user-scope isolation bug.
 *
 * The write side (capture) persists facts under the fallback user scope
 * (`global`). Tools must query under the SAME user scope, otherwise memory
 * written in one session is invisible in another (each session has a distinct
 * id). The session id must still flow through as `session_id` for provenance.
 */

interface FakeBridge {
  call: ReturnType<typeof vi.fn>
}

function setup(extract?: (text: string) => Promise<unknown[]>) {
  const bridge: FakeBridge = {
    call: vi.fn(),
  }
  const registered: ToolDefinition[] = []
  const tools = {
    register: (def: ToolDefinition) => {
      registered.push(def)
      return () => {}
    },
  }
  const deps = {
    ctx: { tools } as any,
    bridge: bridge as any,
    fallbackScope: 'global',
    maxRecalledFacts: 10,
    summaryTokens: 500,
    extract: extract as any,
  }
  registerMemoryTools(deps)
  return { bridge, registered }
}

function execWithSession(sessionId: string | undefined) {
  return {
    agent: sessionId === undefined ? undefined : { session: { id: sessionId } },
  } as any
}

describe('memory tools user scope', () => {
  it('memory_add persists under the fallback user scope with the session id for provenance', async () => {
    const { bridge, registered } = setup()
    const add = registered.find((d) => d.name === 'memory_add')!
    expect(add).toBeTruthy()

    await add.execute({ content: '用户叫小强哥' }, execWithSession('session-AAA'))
    const [method] = bridge.call.mock.calls.at(-1) as [string, Record<string, unknown>]
    expect(method).toBe('add')
    // user_id must be the stable global scope, NOT the session id.
    expect(bridge.call.mock.calls.at(-1)![1]!.user_id).toBe('global')
    // session id still recorded for provenance.
    expect(bridge.call.mock.calls.at(-1)![1]!.session_id).toBe('session-AAA')
  })

  it('memory_recall queries under the fallback user scope regardless of session', async () => {
    const { bridge, registered } = setup()
    const recall = registered.find((d) => d.name === 'memory_recall')!
    bridge.call.mockResolvedValue({ facts: [] })

    await recall.execute({ query: '我是谁' }, execWithSession('session-BBB'))
    const [, params] = bridge.call.mock.calls.at(-1) as [string, Record<string, unknown>]
    expect(params.user_id).toBe('global')

    // A second session must still resolve to the same global user scope —
    // this is the exact bug: per-session user_id made cross-session recall empty.
    await recall.execute({ query: '我是谁' }, execWithSession('session-CCC'))
    expect(bridge.call.mock.calls.at(-1)![1]!.user_id).toBe('global')
  })

  it('memory_recall renders the full detail: fact_id, type and knowledge body', async () => {
    const { bridge, registered } = setup()
    const recall = registered.find((d) => d.name === 'memory_recall')!
    bridge.call.mockResolvedValue({
      facts: [
        {
          fact_id: 'f-lesson-1', subject: '用户', predicate: '教训',
          object: '先备份再升级', type: 'lesson',
          content: '升级前先完整备份数据库。',
        },
        { fact_id: 'f-name-1', subject: '用户', predicate: '名字', object: '小强哥', type: 'semantic', content: null },
      ],
      token_count: 20,
    })

    const result = (await recall.execute({ query: '升级' }, execWithSession('session-XXX'))) as any
    expect(result.facts).toHaveLength(2)

    const rendered = recall.output!.render!({ query: '升级' }, result) as Array<{ text: string }>
    const text = rendered[0]!.text
    // the knowledge BODY must reach the model (a render that only showed the
    // SPO title would hide exactly what recall exists to retrieve)
    expect(text).toContain('升级前先完整备份数据库。')
    expect(text).toContain('(lesson)')
    expect(text).toContain('[f-lesson-1]')
    // a fact without content still renders as a single SPO line
    expect(text).toContain('- [f-name-1] 用户名字: 小强哥 *(semantic)*')
  })

  it('memory_summary fetches the injected compact summary under the fallback scope', async () => {
    const { bridge, registered } = setup()
    const summary = registered.find((d) => d.name === 'memory_summary')!
    expect(summary).toBeTruthy()
    bridge.call.mockResolvedValue('决策规则\n- 一条规则\n\n属性: 工程师')

    const result = (await summary.execute({}, execWithSession('session-YYY'))) as any
    const [method, params] = bridge.call.mock.calls.at(-1) as [string, Record<string, unknown>]
    expect(method).toBe('summary')
    expect(params.user_id).toBe('global')
    // The injected summary is the compact depth (no fact_ids).
    expect(params.detail).toBe(false)
    expect(result.text).toContain('属性: 工程师')

    const rendered = summary.output!.render!({}, result) as Array<{ text: string }>
    expect(rendered[0]!.text).toContain('决策规则')
  })

  it('memory_stats / memory_user_md / memory_summary use the fallback user scope', async () => {
    const { bridge, registered } = setup()
    bridge.call.mockResolvedValue({})

    const stats = registered.find((d) => d.name === 'memory_stats')!
    await stats.execute({}, execWithSession('session-DDD'))
    expect(bridge.call.mock.calls.at(-1)![1]!.user_id).toBe('global')

    const userMd = registered.find((d) => d.name === 'memory_user_md')!
    await userMd.execute({}, execWithSession('session-DDD'))
    expect(bridge.call.mock.calls.at(-1)![1]!.user_id).toBe('global')

    const summary = registered.find((d) => d.name === 'memory_summary')!
    await summary.execute({ detail: true }, execWithSession('session-DDD'))
    expect(bridge.call.mock.calls.at(-1)![1]!.user_id).toBe('global')
  })

  it('memory_summary detail=true asks for the detail depth, not the injected compact one', async () => {
    const { bridge, registered } = setup()
    bridge.call.mockResolvedValue('')

    const summary = registered.find((d) => d.name === 'memory_summary')!
    await summary.execute({ detail: true }, execWithSession('session-DDD-2'))

    // The tool/settings view keeps fact_id references; only the frozen prompt
    // snapshot renders the compact digest.
    const [method, params] = bridge.call.mock.calls.at(-1) as [string, Record<string, unknown>]
    expect(method).toBe('summary')
    expect(params.detail).toBe(true)
  })

  it('memory_summary detail=true does not pass the overview head', async () => {
    // `generate_summary` returns the detail depth before it ever consults
    // `use_overview`, so passing it would be a dead argument that reads like a
    // promise. The two depths stay distinguishable by their params.
    const { bridge, registered } = setup()
    bridge.call.mockResolvedValue('')

    const summary = registered.find((d) => d.name === 'memory_summary')!
    await summary.execute({ detail: true }, execWithSession('session-DDD-3'))
    const detailParams = bridge.call.mock.calls.at(-1)![1] as Record<string, unknown>
    expect(detailParams.overview).toBeUndefined()

    await summary.execute({}, execWithSession('session-DDD-3'))
    const compactParams = bridge.call.mock.calls.at(-1)![1] as Record<string, unknown>
    expect(compactParams.overview).toBe(true)
    expect(compactParams.detail).toBe(false)
  })

  it('memory_summary asks for the overview head it claims to mirror', async () => {
    // The tool's stated purpose is "show what the model is being told". A render
    // without the head would make it disagree with the prompt it mirrors.
    const { bridge, registered } = setup()
    bridge.call.mockResolvedValue('## 以前做过的工作\n- 做过 A\n## 决策规则\n- 一条规则')
    const summary = registered.find((d) => d.name === 'memory_summary')!
    const result = (await summary.execute({}, execWithSession('s1'))) as any
    const params = bridge.call.mock.calls.at(-1)![1] as Record<string, unknown>
    expect(params.overview).toBe(true)
    expect(result.text).toContain('以前做过的工作')
  })

  it('memory_summary tolerates a plain-string reply from an older Python side', async () => {
    const { bridge, registered } = setup()
    bridge.call.mockResolvedValue('旧版纯字符串')
    const summary = registered.find((d) => d.name === 'memory_summary')!
    const result = (await summary.execute({}, execWithSession('s1'))) as any
    expect(result.text).toBe('旧版纯字符串')
  })

  it('an explicit user argument overrides the fallback scope', async () => {
    const { bridge, registered } = setup()
    const recall = registered.find((d) => d.name === 'memory_recall')!
    bridge.call.mockResolvedValue({ facts: [] })

    await recall.execute({ query: 'x', user: 'alice' }, execWithSession('session-EEE'))
    expect(bridge.call.mock.calls.at(-1)![1]!.user_id).toBe('alice')
  })
})

describe('memory_add LLM-first extraction', () => {
  const candidates = [
    { subject: '用户', predicate: '决策', object: '取消关键词门控', type: 'decision_rule' },
  ]

  it('persists typed candidates via persist_candidates when the LLM path yields them', async () => {
    const extract = vi.fn(async () => candidates)
    const { bridge, registered } = setup(extract)
    bridge.call.mockResolvedValue({ candidate_id: 'cand-1' })

    const add = registered.find((d) => d.name === 'memory_add')!
    const result = (await add.execute({ content: '任意自由文本' }, execWithSession('session-FFF'))) as any

    expect(extract).toHaveBeenCalledWith('任意自由文本')
    const [method, params] = bridge.call.mock.calls.at(-1) as [string, Record<string, unknown>]
    // Must NOT go through the rules-only `add` path (which drops free-form text).
    expect(method).toBe('persist_candidates')
    // An explicit remember is the strongest durability signal, so the tool sets
    // an importance floor: without a signal every fact ties and the ordered
    // memory view degenerates to recency.
    expect(params.candidates).toEqual([
      { ...candidates[0], importance: 0.9, confidence: 0.9 },
    ])
    expect(params.user_id).toBe('global')
    expect(params.session_id).toBe('session-FFF')
    expect(result.candidate_id).toBe('cand-1')
  })

  it('never lowers an importance the extractor already ranked higher', async () => {
    const extract = vi.fn(async () => [
      { subject: '用户', predicate: '决定', object: '关键规则', importance: 0.95 },
    ])
    const { bridge, registered } = setup(extract)
    bridge.call.mockResolvedValue({ candidate_id: 'cand-hi' })

    const add = registered.find((d) => d.name === 'memory_add')!
    await add.execute({ content: '关键规则' }, execWithSession('session-HHH2'))

    const [, params] = bridge.call.mock.calls.at(-1) as [string, Record<string, unknown>]
    expect((params.candidates as any[])[0]!.importance).toBe(0.95)
  })

  it('falls back to the rule path when the LLM returns no candidates', async () => {
    const extract = vi.fn(async () => [])
    const { bridge, registered } = setup(extract)
    bridge.call.mockResolvedValue({ candidate_id: 'cand-2' })

    const add = registered.find((d) => d.name === 'memory_add')!
    await add.execute({ content: '我的发布流程是首先构建然后部署' }, execWithSession('session-GGG'))

    const [method, params] = bridge.call.mock.calls.at(-1) as [string, Record<string, unknown>]
    expect(method).toBe('add')
    expect(params.text).toBe('我的发布流程是首先构建然后部署')
  })

  it('falls back to the rule path when the LLM path throws', async () => {
    const extract = vi.fn(async () => { throw new Error('provider down') })
    const { bridge, registered } = setup(extract)
    bridge.call.mockResolvedValue({ candidate_id: 'cand-3' })

    const add = registered.find((d) => d.name === 'memory_add')!
    await add.execute({ content: '用户喜欢黑咖啡' }, execWithSession('session-HHH'))

    const [method] = bridge.call.mock.calls.at(-1) as [string, Record<string, unknown>]
    expect(method).toBe('add')
  })
})

describe('memory_add raw knowledge fallback', () => {
  // Long enough to count as substantial content (>= RAW_KNOWLEDGE_MIN_CHARS).
  const longBody = '运维手册\n' + '部署前先备份数据库，再执行迁移脚本。'.repeat(10)

  it('stores substantial content verbatim when extraction yields nothing', async () => {
    const { bridge, registered } = setup(vi.fn(async () => []))
    bridge.call.mockResolvedValue({ candidate_id: 'cand-raw' })

    const add = registered.find((d) => d.name === 'memory_add')!
    const result = (await add.execute({ content: longBody }, execWithSession('session-III'))) as any

    const [method, params] = bridge.call.mock.calls.at(-1) as [string, Record<string, unknown>]
    // Must NOT fall through to the rules-only `add`, which drops free-form text.
    expect(method).toBe('persist_candidates')
    const candidate = (params.candidates as any[])[0]!
    expect(candidate.type).toBe('sop')              // long-form bucket
    expect(candidate.content).toBe(longBody.trim()) // full body preserved
    expect(candidate.object).toBe('运维手册')        // title from the first line
    expect(result.fallback).toBe('raw')
  })

  it('also rescues substantial content when the extraction call throws', async () => {
    const { bridge, registered } = setup(vi.fn(async () => { throw new Error('truncated') }))
    bridge.call.mockResolvedValue({ candidate_id: 'cand-raw2' })

    const add = registered.find((d) => d.name === 'memory_add')!
    await add.execute({ content: longBody }, execWithSession('session-JJJ'))

    const [method] = bridge.call.mock.calls.at(-1) as [string, Record<string, unknown>]
    expect(method).toBe('persist_candidates')
  })

  it('leaves short utterances on the rule path', async () => {
    const { bridge, registered } = setup(vi.fn(async () => []))
    bridge.call.mockResolvedValue({ candidate_id: 'cand-short' })

    const add = registered.find((d) => d.name === 'memory_add')!
    await add.execute({ content: '用户喜欢黑咖啡' }, execWithSession('session-KKK'))

    const [method] = bridge.call.mock.calls.at(-1) as [string, Record<string, unknown>]
    expect(method).toBe('add')
  })
})

// ---- memory_overview ---------------------------------------------------------
//
// The user-facing half of the changelog. Two things are worth pinning here:
// that `show` returns the overview *head* and not the digest `memory_summary`
// already returns, and that a refresh is never attempted when the deployment has
// no refresher wired — appearing to work is worse than refusing.

describe('memory_overview tool', () => {
  function setupOverview(extra: Record<string, unknown> = {}) {
    const bridge = { call: vi.fn() }
    const registered: ToolDefinition[] = []
    const tools = {
      register: (def: ToolDefinition) => {
        registered.push(def)
        return () => {}
      },
    }
    registerMemoryTools({
      ctx: { tools } as any,
      bridge: bridge as any,
      fallbackScope: 'global',
      maxRecalledFacts: 10,
      summaryTokens: 500,
      ...extra,
    } as any)
    return { bridge, registered, tool: registered.find(d => d.name === 'memory_overview')! }
  }

  it('is registered', () => {
    expect(setupOverview().tool).toBeTruthy()
  })

  it('show returns only the overview head, not the digest', async () => {
    const { bridge, tool } = setupOverview()
    bridge.call.mockResolvedValue({
      text: [
        '## 以前做过的工作',
        '- 做过 A 和 B',
        '## 决策规则',
        '- 一条规则',
      ].join('\n'),
    })
    const result = (await tool.execute({}, execWithSession('s1'))) as any
    expect(result.text).toContain('以前做过的工作')
    expect(result.text).toContain('做过 A 和 B')
    // The digest belongs to `memory_summary`; repeating it would make the two
    // tools indistinguishable to the model. The head ends at the detail's first
    // section label.
    expect(result.text).not.toContain('决策规则')

    const params = bridge.call.mock.calls.at(-1)![1] as Record<string, unknown>
    expect(params.overview).toBe(true)
    expect(params.detail).toBe(false)
  })

  it('show does not mistake a multi-line overview for the digest', async () => {
    // The head is model-written prose and may itself carry `#`-prefixed lines or
    // blank lines; only a `## ` label *after* the head's own line starts the
    // detail, so multi-line prose must survive whole.
    const { bridge, tool } = setupOverview()
    bridge.call.mockResolvedValue({
      text: [
        '## 以前做过的工作',
        '- 第一项',
        '',
        '- 第二项',
        '## 教训',
        '- 一条教训',
      ].join('\n'),
    })
    const result = (await tool.execute({}, execWithSession('s1'))) as any
    expect(result.text).toContain('第一项')
    expect(result.text).toContain('第二项')
    expect(result.text).not.toContain('教训')
  })

  it('show falls back to the whole text when there is no head', async () => {
    // An empty store, or an older Python side: say what there is rather than
    // returning an empty string under a heading that would misdescribe it.
    const { bridge, tool } = setupOverview()
    bridge.call.mockResolvedValue('## 决策规则\n- 一条规则')
    const result = (await tool.execute({}, execWithSession('s1'))) as any
    expect(result.text).toContain('决策规则')
  })

  it('status reports the cache state and the refresh verdict', async () => {
    const { bridge, tool } = setupOverview()
    bridge.call.mockResolvedValue({
      cached: false,
      stale: true,
      should_refresh: false,
      refresh_reason: 'no_facts',
    })
    const result = (await tool.execute({ action: 'status' }, execWithSession('s1'))) as any
    expect(result.text).toContain('尚无缓存')
    expect(result.text).toContain('记忆库为空')
    // `status` also fetches the consistency report, so assert the call was made
    // rather than relying on it being the last one.
    expect(bridge.call.mock.calls.map(c => c[0])).toContain('overview_status')
  })

  it('changes renders the changelog with readable labels', async () => {
    const { bridge, tool } = setupOverview()
    bridge.call.mockResolvedValue({
      changes: [
        { type: 'fact_written', created_at: Date.UTC(2026, 1, 5, 3, 4), detail: { predicate: '决定', type: 'decision_rule' } },
        { type: 'fact_superseded', created_at: 0, detail: {} },
      ],
      level: 'structural',
    })
    const result = (await tool.execute({ action: 'changes' }, execWithSession('s1'))) as any
    expect(result.text).toContain('变动级别：structural')
    expect(result.text).toContain('写入')
    expect(result.text).toContain('决定')
    expect(result.text).toContain('替换')
    expect(result.text).toContain('?')  // the zero timestamp
  })

  it('changes says so when nothing happened', async () => {
    const { bridge, tool } = setupOverview()
    bridge.call.mockResolvedValue({ changes: [] })
    const result = (await tool.execute({ action: 'changes' }, execWithSession('s1'))) as any
    expect(result.text).toContain('没有记忆变动')
  })

  it('changes forwards since/limit only when they parse', async () => {
    const { bridge, tool } = setupOverview()
    bridge.call.mockResolvedValue({ changes: [] })
    await tool.execute({ action: 'changes', since: '1234', limit: '5' }, execWithSession('s1'))
    let params = bridge.call.mock.calls.at(-1)![1] as Record<string, unknown>
    expect(params.since_ms).toBe(1234)
    expect(params.limit).toBe(5)

    await tool.execute({ action: 'changes', since: 'nonsense' }, execWithSession('s1'))
    params = bridge.call.mock.calls.at(-1)![1] as Record<string, unknown>
    expect(params).not.toHaveProperty('since_ms')
  })

  it('refresh calls the wired refresher exactly once', async () => {
    const refreshOverview = vi.fn(async () => 'refreshed')
    const { tool } = setupOverview({ refreshOverview })
    const result = (await tool.execute({ action: 'refresh' }, execWithSession('s1'))) as any
    expect(refreshOverview).toHaveBeenCalledTimes(1)
    expect(result.text).toContain('已重新生成')
  })

  it('refresh refuses honestly when no refresher is wired', async () => {
    const { tool } = setupOverview()
    const result = (await tool.execute({ action: 'refresh' }, execWithSession('s1'))) as any
    expect(result.text).toContain('未启用')
  })

  it('refresh translates every outcome token', async () => {
    for (const [outcome, needle] of [
      ['throttled', '太近'],
      ['no-model', '未配置可用模型'],
      ['nothing-to-narrate', '暂无可叙述'],
      ['empty-generation', '没有产出内容'],
      ['skipped', '未启用'],
      ['error', '生成失败'],
      ['no-change:up_to_date', '仅细节变化'],
      ['no-change:level', '无需重新生成'],
    ] as Array<[string, string]>) {
      const { tool } = setupOverview({ refreshOverview: async () => outcome })
      const result = (await tool.execute({ action: 'refresh' }, execWithSession('s1'))) as any
      expect(result.text).toContain(needle)
    }
  })

  it('rejects an unknown action rather than silently defaulting to show', async () => {
    const { tool } = setupOverview()
    await expect(tool.execute({ action: 'nope' }, execWithSession('s1')))
      .rejects.toThrow(/unknown action/)
  })

  it('defaults to show', async () => {
    const { bridge, tool } = setupOverview()
    bridge.call.mockResolvedValue({ text: '## 以前做过的工作\n- A' })
    await tool.execute({}, execWithSession('s1'))
    expect(bridge.call.mock.calls.at(-1)![0]).toBe('summary')
  })

  it('honours an explicit user argument', async () => {
    const { bridge, tool } = setupOverview()
    bridge.call.mockResolvedValue({ text: '## 以前做过的工作\n- A' })
    await tool.execute({ user: 'someone-else' }, execWithSession('s1'))
    expect(bridge.call.mock.calls.at(-1)![1]!.user_id).toBe('someone-else')
  })
})

describe('memory_domains queue actions', () => {
  function setupDomains() {
    const { bridge, registered } = setup()
    return { bridge, tool: registered.find(d => d.name === 'memory_domains')! }
  }

  it('signal_promote forwards the name and renders the registration', async () => {
    const { bridge, tool } = setupDomains()
    bridge.call.mockResolvedValue({
      domain_id: 7, name: 'teaching', path: 'teaching', already_registered: false,
    })

    const result = (await tool.execute(
      { action: 'signal_promote', name: 'teaching' },
      execWithSession('s1'),
    )) as any

    const [method, params] = bridge.call.mock.calls.at(-1) as [string, Record<string, unknown>]
    expect(method).toBe('domain_signal_promote')
    expect(params.name).toBe('teaching')
    expect(params.user_id).toBe('global')

    const text = (tool.output!.render!({ action: 'signal_promote' }, result) as Array<{ text: string }>)[0]!.text
    expect(text).toContain('已注册主题')
    expect(text).toContain('teaching')
  })

  it('signal_promote reports a pre-existing topic as already registered', async () => {
    const { bridge, tool } = setupDomains()
    bridge.call.mockResolvedValue({
      domain_id: 7, name: 'teaching', path: 'teaching', already_registered: true,
    })

    const result = (await tool.execute(
      { action: 'signal_promote', name: 'teaching' },
      execWithSession('s1'),
    )) as any
    const text = (tool.output!.render!({ action: 'signal_promote' }, result) as Array<{ text: string }>)[0]!.text

    // Not an error: the queue row was still cleared, so it must not read as a
    // failure the caller should react to.
    expect(text).toContain('已存在')
  })

  it('signal_promote requires a name', async () => {
    const { bridge, tool } = setupDomains()

    await expect(tool.execute({ action: 'signal_promote' }, execWithSession('s1')))
      .rejects.toThrow(/requires name/)
    expect(bridge.call).not.toHaveBeenCalled()
  })

  it('advertises signal_promote in the action list, so it is reachable at all', async () => {
    const { tool } = setupDomains()
    const described = String((tool.parameters as any).properties.action.description)

    // The original defect was an action that existed in Python and the RPC name
    // table but appeared in no action list — unreachable, so the queue never
    // drained. This pins the advertisement, not just the implementation.
    expect(described).toContain('signal_promote')
    expect(String(tool.description)).toContain('signal_promote')
  })

  it('relabel_from_scopes is advertised and defaults to a dry run', async () => {
    const { bridge, tool } = setupDomains()
    bridge.call.mockResolvedValue({ dry_run: true, candidates: 0, added: {} })

    const described = String((tool.parameters as any).properties.action.description)
    expect(described).toContain('relabel_from_scopes')

    // An omitted argument must not rewrite labels: the caller has to opt in.
    await tool.execute({ action: 'relabel_from_scopes' }, execWithSession('s1'))
    let [, params] = bridge.call.mock.calls.at(-1) as [string, Record<string, unknown>]
    expect(params.dry_run).toBe(true)

    await tool.execute(
      { action: 'relabel_from_scopes', dryRun: false },
      execWithSession('s1'),
    )
    ;[, params] = bridge.call.mock.calls.at(-1) as [string, Record<string, unknown>]
    expect(params.dry_run).toBe(false)
  })

  it('relabel_from_scopes forwards a sane limit', async () => {
    const { bridge, tool } = setupDomains()
    bridge.call.mockResolvedValue({ dry_run: true, candidates: 0, added: {} })

    const cases: Array<[Record<string, unknown>, number]> = [
      // Omitted, explicitly 0, and negative all mean "no cap"; a positive value
      // is truncated to an integer.
      [{}, 0],
      [{ limit: 0 }, 0],
      [{ limit: -5 }, 0],
      [{ limit: 10 }, 10],
    ]
    for (const [extra, expected] of cases) {
      await tool.execute(
        { action: 'relabel_from_scopes', ...extra },
        execWithSession('s1'),
      )
      const [, params] = bridge.call.mock.calls.at(-1) as [string, Record<string, unknown>]
      expect(params.limit).toBe(expected)
    }
  })

  it('relabel_from_scopes renders the plan it is previewing', async () => {
    const { bridge, tool } = setupDomains()
    bridge.call.mockResolvedValue({
      dry_run: true, mapped_scopes: 3, candidates: 12, applied: 0,
      added: { teaching: 8, research: 4 },
    })

    const result = (await tool.execute(
      { action: 'relabel_from_scopes' },
      execWithSession('s1'),
    )) as any
    const text = (tool.output!.render!(
      { action: 'relabel_from_scopes' },
      result,
    ) as Array<{ text: string }>)[0]!.text

    expect(text).toContain('预演')
    expect(text).toContain('12')
    expect(text).toContain('teaching：8 条')
    expect(text).toContain('dryRun=false')
  })

  it('relabel_from_scopes names the missing mapping as the cause of an empty plan', async () => {
    const { bridge, tool } = setupDomains()
    bridge.call.mockResolvedValue({
      dry_run: true, mapped_scopes: 0, candidates: 0, applied: 0, added: {},
    })

    const result = (await tool.execute(
      { action: 'relabel_from_scopes' },
      execWithSession('s1'),
    )) as any
    const text = (tool.output!.render!(
      { action: 'relabel_from_scopes' },
      result,
    ) as Array<{ text: string }>)[0]!.text

    // "Nothing to do" has two very different causes; naming the wrong one sends
    // the user looking for a bug in the wrong place.
    expect(text).toContain('scopeDomainMap')
  })
})

describe('capacity and detection visibility', () => {
  it('says the capacity policy is off instead of showing a silent zero', () => {
    const text = renderStats({
      facts: 1195, pending: 0, archived: 0,
      capacity: { cap: 0, enabled: false, active: 1195, archivable: 0, protected: 1195 },
    })
    expect(text).toContain('未启用')
  })

  it('explains that everything is protected when the cap cannot be reached', () => {
    // The live store's exact state: over cap, nothing movable.
    const text = renderStats({
      facts: 1195, pending: 0, archived: 0,
      capacity: {
        cap: 1000, enabled: true, active: 1195, archivable: 0, protected: 1195,
        oldest_age_days: 10, protect_days: 14,
      },
    })
    expect(text).toContain('无可归档')
    expect(text).toContain('14')
  })

  it('renders without a capacity block from an older store', () => {
    expect(renderStats({ facts: 3, pending: 0, archived: 0 })).toContain('活跃记忆 3 条')
  })

  it('reports detection findings through overview status', () => {
    const text = renderOverviewStatus(
      { cached: true, level: 'none', refresh_reason: 'up_to_date' },
      { counts: { duplicates: 1, conflicts: 0 } },
    )
    expect(text).toContain('1')
    expect(text).toContain('待处置')
  })

  it('stays silent when detection found nothing', () => {
    const text = renderOverviewStatus(
      { cached: true, level: 'none', refresh_reason: 'up_to_date' },
      { counts: { duplicates: 0, conflicts: 0 } },
    )
    expect(text).not.toContain('待处置')
  })

  it('renders status with no report at all', () => {
    expect(renderOverviewStatus({ cached: true })).toContain('总览')
  })
})

describe('memory_overview status reports findings', () => {
  function setupOverview() {
    const bridge = { call: vi.fn() }
    const registered: ToolDefinition[] = []
    const tools = {
      register: (def: ToolDefinition) => {
        registered.push(def)
        return () => {}
      },
    }
    registerMemoryTools({
      ctx: { tools } as any,
      bridge: bridge as any,
      fallbackScope: 'global',
      maxRecalledFacts: 10,
      summaryTokens: 500,
    } as any)
    return { bridge, tool: registered.find(d => d.name === 'memory_overview')! }
  }

  it('fetches the report and names the disposal path', async () => {
    const { bridge, tool } = setupOverview()
    bridge.call.mockImplementation(async (method: string) => {
      if (method === 'overview_status') return { cached: true, refresh_reason: 'up_to_date' }
      if (method === 'consolidation_report') return { counts: { duplicates: 2, conflicts: 1 } }
      return {}
    })
    const result = (await tool.execute({ action: 'status' }, execWithSession('s1'))) as any
    expect(result.text).toContain('待处置')
    expect(result.text).toContain('memory_replace')
  })

  it('still renders status when the store predates the report method', async () => {
    const { bridge, tool } = setupOverview()
    bridge.call.mockImplementation(async (method: string) => {
      if (method === 'overview_status') return { cached: true, refresh_reason: 'up_to_date' }
      throw new Error('unknown method: consolidation_report')
    })
    const result = (await tool.execute({ action: 'status' }, execWithSession('s1'))) as any
    expect(result.text).toContain('总览')
  })
})
