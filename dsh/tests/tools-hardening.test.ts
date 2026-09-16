/**
 * Tests for the hardening round's dsh-side surface.
 *
 * Three properties, all of them about the model not being misled:
 *
 *  - a shortened write says it was shortened (and a merged one says it merged);
 *  - `memory_get` gives the model the way to read a body recall shortened;
 *  - the start retry actually backs off, as the README always claimed.
 */
import { describe, expect, it, vi } from 'vitest'
import type { ToolDefinition } from '@deepseek-ai/dsh-tools'
import { registerMemoryTools, renderWriteReceipt } from '../src/tools.ts'
import { retryDelayMs } from '../src/index.ts'

function setup() {
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
  })
  const tool = (name: string) => registered.find(d => d.name === name)!
  const render = (name: string, value: unknown): string => {
    const out = tool(name).output as { render: (a: unknown, v: unknown) => Array<{ text: string }> }
    return out.render({}, value)[0]!.text
  }
  return { bridge, registered, tool, render }
}

const execWithSession = (id = 's1') => ({ agent: { session: { id } } }) as any

describe('renderWriteReceipt: shortening is reported', () => {
  it('says how much was kept when the store had to cap the text', () => {
    const text = renderWriteReceipt({
      status: 'applied',
      outcome: {
        written: ['f1'],
        truncated: [{ field: 'content', original_chars: 40_000, kept_chars: 20_000 }],
      },
    })
    expect(text).toContain('已记住 1 条')
    expect(text).toContain('截断')
    expect(text).toContain('20000')
    expect(text).toContain('40000')
  })

  it('reports truncation on the enqueue receipt too, before any verdict exists', () => {
    const text = renderWriteReceipt({
      status: 'pending',
      outcome: { truncated: [{ field: 'content', original_chars: 900, kept_chars: 500 }] },
    })
    expect(text).toContain('已入队')
    expect(text).toContain('截断')
  })

  it('stays silent about truncation when nothing was shortened', () => {
    const text = renderWriteReceipt({ status: 'applied', outcome: { written: ['f1'], truncated: [] } })
    expect(text).not.toContain('截断')
  })
})

describe('renderWriteReceipt: a merge is not a write', () => {
  it('distinguishes an exact repeat from a semantic one', () => {
    const text = renderWriteReceipt({
      status: 'applied',
      outcome: {
        written: [],
        reinforced: [
          { fact_id: 'f1', on: 'fingerprint' },
          { fact_id: 'f2', on: 'embedding' },
        ],
      },
    })
    expect(text).toContain('重复')
    expect(text).toContain('1 条内容完全相同')
    expect(text).toContain('1 条语义近似')
  })

  it('treats the SPO-exact test as an exact match, not a semantic one', () => {
    const text = renderWriteReceipt({
      status: 'applied',
      outcome: { written: [], reinforced: [{ fact_id: 'f1', on: 'idempotent' }] },
    })
    expect(text).toContain('1 条内容完全相同')
    expect(text).not.toContain('语义近似')
  })
})

describe('memory_get', () => {
  it('reads one fact by id, under the shared user scope', async () => {
    const { bridge, tool } = setup()
    bridge.call.mockResolvedValue({
      fact_id: 'f1', subject: '用户', predicate: '知识', object: '部署流程',
      type: 'sop', status: 'active', content: '完整正文……',
    })
    const result = await tool('memory_get').execute({ factId: 'f1' }, execWithSession('s9'))
    const [method, params] = bridge.call.mock.calls.at(-1) as [string, Record<string, unknown>]
    expect(method).toBe('get_fact')
    expect(params).toMatchObject({ fact_id: 'f1', user_id: 'global' })
    const out = tool('memory_get').output as { render: (a: unknown, v: unknown) => Array<{ text: string }> }
    const text = out.render({}, result)[0]!.text
    expect(text).toContain('完整正文')
    expect(text).toContain('[f1]')
  })

  it('refuses to run without an id', async () => {
    const { bridge, tool } = setup()
    await expect(tool('memory_get').execute({ factId: '' }, execWithSession()))
      .rejects.toThrow(/factId/)
    expect(bridge.call).not.toHaveBeenCalled()
  })
})

describe('memory_recall', () => {
  it('flags a shortened body and points at memory_get', async () => {
    const { bridge, tool, render } = setup()
    bridge.call.mockResolvedValue({
      facts: [{
        fact_id: 'f1', subject: '用户', predicate: '知识', object: '部署流程',
        type: 'sop', content: '被截断的正文…', truncated: true,
      }],
    })
    const result = await tool('memory_recall').execute({ query: '部署' }, execWithSession())
    const text = render('memory_recall', result)
    expect(text).toContain('正文已截断')
    expect(text).toContain('memory_get factId=f1')
  })

  it('says nothing about truncation for a complete body', async () => {
    const { bridge, tool, render } = setup()
    bridge.call.mockResolvedValue({
      facts: [{ fact_id: 'f1', subject: '用户', predicate: '偏好', object: '黑咖啡', truncated: false }],
    })
    const result = await tool('memory_recall').execute({ query: '咖啡' }, execWithSession())
    expect(render('memory_recall', result)).not.toContain('已截断')
  })
})

describe('bridge start backoff', () => {
  it('doubles the delay per attempt and then holds at the ceiling', () => {
    expect(retryDelayMs(1)).toBe(1_000)
    expect(retryDelayMs(2)).toBe(2_000)
    expect(retryDelayMs(3)).toBe(4_000)
    // Capped: a fourth attempt does not wait 8s and beyond forever.
    expect(retryDelayMs(5)).toBe(15_000)
    expect(retryDelayMs(50)).toBe(15_000)
  })

  it('treats a nonsense attempt number as the first attempt', () => {
    expect(retryDelayMs(0)).toBe(1_000)
    expect(retryDelayMs(-3)).toBe(1_000)
  })
})
