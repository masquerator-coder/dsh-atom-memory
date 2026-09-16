/**
 * Tests for the write-outcome surface of the memory tools.
 *
 * The store resolves contradictions itself (a newer assertion replaces a stored
 * value; a weaker one is refused), so the tool's report is the only place the
 * model can learn which of the two happened. A memory tool that renders every
 * write as success teaches the model to trust a store that is not there.
 */
import { describe, expect, it, vi } from 'vitest'
import type { ToolDefinition } from '@deepseek-ai/dsh-tools'
import { registerMemoryTools, renderWriteReceipt } from '../src/tools.ts'

function setup(overrides: Record<string, unknown> = {}) {
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
    ...overrides,
  })
  const tool = (name: string) => registered.find(d => d.name === name)!
  const render = (name: string, value: unknown): string => {
    const out = tool(name).output as { render: (a: unknown, v: unknown) => Array<{ text: string }> }
    return out.render({}, value)[0]!.text
  }
  return { bridge, registered, tool, render }
}

const execWithSession = (id = 's1') => ({ agent: { session: { id } } }) as any

describe('renderWriteReceipt', () => {
  it('reports a plain write as a write', () => {
    expect(renderWriteReceipt({ status: 'applied', outcome: { written: ['f1'] } }))
      .toBe('已记住 1 条。')
  })

  it('reports a supersede with both values, so the model can correct itself', () => {
    const text = renderWriteReceipt({
      status: 'applied',
      outcome: {
        written: ['f2'],
        superseded: [{ predicate: '职业', old_object: '工程师', new_object: '设计师' }],
      },
    })
    expect(text).toContain('职业')
    expect(text).toContain('工程师 → 设计师')
  })

  it('reports a refusal with the reason instead of claiming success', () => {
    const text = renderWriteReceipt({
      status: 'skipped',
      outcome: { rejected: [{ kind: 'conflict', detail: '已存记忆的证据更强（confidence 0.60 对 0.90）' }] },
    })
    expect(text).toContain('拒绝 1 条')
    expect(text).toContain('证据更强')
  })

  it('distinguishes a queued write from a stored one', () => {
    expect(renderWriteReceipt({ status: 'pending' })).toContain('已入队')
  })

  it('reports a failed write as a failure, not as an enqueue', () => {
    const text = renderWriteReceipt({ status: 'error', reject_reason: 'vector dimension mismatch' })
    expect(text).toContain('写入失败')
    expect(text).toContain('vector dimension mismatch')
  })

  it('distinguishes a soft forget from a purge', () => {
    expect(renderWriteReceipt({ status: 'applied', outcome: { retracted: ['f1'] } })).toContain('已遗忘')
    expect(renderWriteReceipt({ status: 'applied', outcome: { purged: ['f1'] } })).toContain('彻底删除')
  })
})

describe('write tools', () => {
  it('memory_add asks for the verdict and renders it', async () => {
    const { bridge, tool, render } = setup()
    bridge.call.mockResolvedValue({
      status: 'applied',
      outcome: { written: ['f1'], superseded: [{ predicate: '常用颜色', old_object: '蓝色', new_object: '绿色' }] },
    })
    const add = tool('memory_add')
    const result = await add.execute({ content: '我的常用颜色是绿色' }, execWithSession())
    const [method, params] = bridge.call.mock.calls.at(-1) as [string, Record<string, unknown>]
    expect(method).toBe('add')
    expect(params.wait_ms).toBe(2500)
    expect(render('memory_add', result)).toContain('蓝色 → 绿色')
  })

  it('memory_replace names the fact it retires', async () => {
    const { bridge, tool } = setup()
    bridge.call.mockResolvedValue({ status: 'applied', outcome: { written: ['f9'] } })
    await tool('memory_replace').execute({ factId: 'f1', content: '职业改为设计师' }, execWithSession())
    const [method, params] = bridge.call.mock.calls.at(-1) as [string, Record<string, unknown>]
    expect(method).toBe('replace')
    expect(params).toMatchObject({ fact_id: 'f1', new_text: '职业改为设计师', wait_ms: 2500, user_id: 'global' })
  })

  it('memory_replace refuses to run without a target', async () => {
    const { bridge, tool } = setup()
    await expect(tool('memory_replace').execute({ factId: '', content: 'x' }, execWithSession()))
      .rejects.toThrow(/factId/)
    expect(bridge.call).not.toHaveBeenCalled()
  })

  it('memory_forget carries the purge flag through', async () => {
    const { bridge, tool } = setup()
    bridge.call.mockResolvedValue({ status: 'applied', outcome: { purged: ['f1'] } })
    await tool('memory_forget').execute({ factId: 'f1', purge: true }, execWithSession())
    const [method, params] = bridge.call.mock.calls.at(-1) as [string, Record<string, unknown>]
    expect(method).toBe('forget')
    expect(params.purge).toBe(true)
  })

  it('memory_recall flags a degraded index instead of implying "no memory"', async () => {
    const { bridge, tool, render } = setup()
    bridge.call.mockResolvedValue({ facts: [], degraded: ['vector'] })
    const result = await tool('memory_recall').execute({ query: '咖啡' }, execWithSession()) as unknown
    const text = render('memory_recall', result)
    expect(text).toContain('索引')
    expect(text).toContain('不完整')
  })
})

describe('memory_snapshot', () => {
  it('returns the frozen text the prompt is actually served from', async () => {
    const peek = vi.fn(() => '===== BEGIN MEMORY-DATA =====\n| - fact')
    const { tool } = setup({ snapshot: { peek, ensure: vi.fn(async () => '') } })
    const result = await tool('memory_snapshot').execute({}, execWithSession('s1')) as { text?: string; frozen?: boolean }
    expect(result.frozen).toBe(true)
    expect(String(result.text)).toContain('| - fact')
    expect(peek).toHaveBeenCalledWith('s1')
  })

  it('freezes on demand when the session has not been assembled yet', async () => {
    const ensure = vi.fn(async () => '===== BEGIN MEMORY-DATA =====\n| - late')
    const { tool, render } = setup({ snapshot: { peek: () => undefined, ensure } })
    const result = await tool('memory_snapshot').execute({}, execWithSession('s2')) as { text?: string; frozen?: boolean }
    expect(ensure).toHaveBeenCalledWith('s2')
    expect(result.frozen).toBe(false)
    expect(render('memory_snapshot', result)).toContain('尚未冻结')
  })

  it('says so when nothing is injected rather than looking like a failure', async () => {
    const { tool } = setup({ snapshot: { peek: () => '', ensure: vi.fn(async () => '') } })
    const result = await tool('memory_snapshot').execute({}, execWithSession('s3')) as { text?: string; frozen?: boolean }
    expect(String(result.text)).toContain('无内容')
  })
})

describe('memory_summary budget', () => {
  it('renders at the injected budget, so the tool and the prompt agree', async () => {
    const { bridge, tool } = setup({ resolveSummaryBudget: () => 800 })
    bridge.call.mockResolvedValue('# Memory')
    await tool('memory_summary').execute({}, execWithSession())
    const [method, params] = bridge.call.mock.calls.at(-1) as [string, Record<string, unknown>]
    expect(method).toBe('summary')
    expect(params.max_tokens).toBe(800)
    expect(params.detail).toBe(false)
  })

  it('keeps the detail view on its own, larger budget', async () => {
    const { bridge, tool } = setup({ resolveSummaryBudget: () => 800 })
    bridge.call.mockResolvedValue('# Memory detail')
    await tool('memory_summary_detail').execute({}, execWithSession())
    expect(bridge.call.mock.calls.at(-1)![1]!.max_tokens).toBe(500)
  })
})
