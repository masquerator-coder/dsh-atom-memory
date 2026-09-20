import { describe, it, expect, vi } from 'vitest'
import { registerCapture } from '../src/capture.ts'

interface FakeSessionEvent {
  type: string
  data: { content?: Array<{ type?: string; text?: string }>; source?: { kind?: string } }
  seq?: number
}

function makeCtx() {
  const byEvent = new Map<string, Array<(...args: any[]) => unknown>>()
  const on = vi.fn((event: string, handler: (...args: any[]) => unknown) => {
    byEvent.set(event, [...(byEvent.get(event) ?? []), handler])
    return () => { /* no-op disposer */ }
  })
  return {
    ctx: { on } as any,
    on,
    handlersOf: (event: string) => byEvent.get(event) ?? [],
  }
}

/** Feed a ``user/message`` durable event to a registered capture hook. */
function send(
  session: string,
  text: string,
  byEvent: ReturnType<typeof makeCtx>['handlersOf'],
  cwd?: string,
) {
  const handler = byEvent('session/event')[0] as (s: unknown, e: FakeSessionEvent) => void
  handler({ id: session, header: cwd === undefined ? undefined : { cwd } }, {
    type: 'user/message',
    data: { content: [{ type: 'text', text }], source: { kind: 'user' } },
    seq: 1,
  })
}

describe('registerCapture', () => {
  it('fires capture for any direct user message (no keyword gate)', async () => {
    const { ctx, handlersOf } = makeCtx()
    const capture = vi.fn(async () => {})
    registerCapture(
      { ctx, capture },
      { captureEnabled: () => true, nudgeEnabled: false, nudgeIntervalMs: 60_000 },
    )
    expect(ctx.on).toHaveBeenCalledWith('session/event', expect.any(Function))
    // No strong-fact keyword present, but capture must still fire.
    send('s1', '总结我的obsidian工作笔记', handlersOf)
    await new Promise(r => setTimeout(r, 10))
    // The third argument is the session's working directory: `undefined` here
    // because this fake session carries no header, and the caller then falls
    // back to its own default.
    expect(capture).toHaveBeenCalledWith('总结我的obsidian工作笔记', 's1', undefined)
  })

  it('passes the session working directory so the write carries a scope context', async () => {
    const { ctx, handlersOf } = makeCtx()
    const capture = vi.fn(async () => {})
    registerCapture(
      { ctx, capture },
      { captureEnabled: () => true, nudgeEnabled: false, nudgeIntervalMs: 60_000 },
    )
    send('s1', '用户偏好黑咖啡', handlersOf, 'D:/work/repo')
    await new Promise(r => setTimeout(r, 10))
    // Which checkout a fact came from is not derivable from the session id, so
    // the hook hands the directory over rather than letting the writer guess.
    expect(capture).toHaveBeenCalledWith('用户偏好黑咖啡', 's1', 'D:/work/repo')
  })

  it('does not fire for plugin-sourced content', async () => {
    const { ctx, handlersOf } = makeCtx()
    const capture = vi.fn(async () => {})
    registerCapture(
      { ctx, capture },
      { captureEnabled: () => true, nudgeEnabled: false, nudgeIntervalMs: 60_000 },
    )
    const handler = handlersOf('session/event')[0] as (s: unknown, e: FakeSessionEvent) => void
    handler({ id: 's1' }, {
      type: 'user/message',
      data: { content: [{ type: 'text', text: '用户喜欢黑咖啡' }], source: { kind: 'plugin' } },
      seq: 1,
    })
    await new Promise(r => setTimeout(r, 10))
    expect(capture).not.toHaveBeenCalled()
  })

  it('does not fire for non-user-message events', async () => {
    const { ctx, handlersOf } = makeCtx()
    const capture = vi.fn(async () => {})
    registerCapture(
      { ctx, capture },
      { captureEnabled: () => true, nudgeEnabled: false, nudgeIntervalMs: 60_000 },
    )
    const handler = handlersOf('session/event')[0] as (s: unknown, e: FakeSessionEvent) => void
    handler({ id: 's1' }, {
      type: 'turn/end',
      data: { content: [{ type: 'text', text: '用户喜欢黑咖啡' }], source: { kind: 'user' } },
      seq: 2,
    })
    await new Promise(r => setTimeout(r, 10))
    expect(capture).not.toHaveBeenCalled()
  })

  it('registers the hooks and honours the switch live, in both directions', async () => {
    const { ctx, handlersOf } = makeCtx()
    const capture = vi.fn(async () => {})
    let enabled = false
    registerCapture(
      { ctx, capture },
      { captureEnabled: () => enabled, nudgeEnabled: false, nudgeIntervalMs: 60_000 },
    )
    // Always registered: the switch is read per event, so turning capture on
    // later takes effect on the next message rather than at the next reload.
    expect(ctx.on).toHaveBeenCalledWith('session/event', expect.any(Function))

    send('s1', '用户偏好黑咖啡', handlersOf)
    await new Promise(r => setTimeout(r, 10))
    expect(capture).not.toHaveBeenCalled()

    enabled = true
    send('s2', '用户喜欢蓝山咖啡', handlersOf)
    await new Promise(r => setTimeout(r, 10))
    expect(capture).toHaveBeenCalledWith('用户喜欢蓝山咖啡', 's2', undefined)
  })

  it('rescues a message whose immediate capture failed, on the next nudge sweep', async () => {
    const { ctx, handlersOf } = makeCtx()
    // First attempt fails (bridge down); the nudge retry succeeds.
    const outcomes: Array<'fail' | 'ok'> = ['fail', 'ok']
    const capture = vi.fn(async () => {
      const next = outcomes.shift()
      if (next === 'fail') throw new Error('bridge down')
    })
    registerCapture(
      { ctx, capture },
      // The nudge is the only retry path: `llm/stream` is no longer intercepted.
      { captureEnabled: () => true, nudgeEnabled: true, nudgeIntervalMs: 1000 },
    )

    send('s1', '用户偏好黑咖啡', handlersOf)
    await new Promise(r => setTimeout(r, 10))
    expect(capture).toHaveBeenCalledTimes(1)
    expect(capture).toHaveBeenCalledWith('用户偏好黑咖啡', 's1', undefined)

    // No compaction hook is registered any more — the retry belongs to the
    // timer alone.
    expect(handlersOf('llm/stream')).toHaveLength(0)

    // Wait for one nudge tick (the interval is clamped to >= 1000 ms).
    await new Promise(r => setTimeout(r, 1200))

    // The failed message was rescued: capture ran twice with the same text and
    // session. The sweep only knows a session id, so it passes no directory and
    // the writer falls back to its own — the same fallback the first attempt used.
    expect(capture).toHaveBeenCalledTimes(2)
    expect(capture.mock.calls[1]).toEqual(['用户偏好黑咖啡', 's1'])
  })

  it('does not retry a message whose capture is still in flight', async () => {
    const { ctx, handlersOf } = makeCtx()
    let settle: (() => void) | undefined
    const capture = vi.fn(() => new Promise<void>((res) => { settle = res }))
    registerCapture(
      { ctx, capture },
      { captureEnabled: () => true, nudgeEnabled: true, nudgeIntervalMs: 1000 },
    )

    send('s1', '采集尚未落地的消息', handlersOf)

    // An in-flight entry is neither captured nor failed, so the sweep leaves it
    // alone rather than double-sending a text the live attempt still owns.
    await new Promise(r => setTimeout(r, 1200))
    expect(capture).toHaveBeenCalledTimes(1)

    settle?.()
  })

  // -- the overview refresher's trigger --------------------------------------
  //
  // `afterPersist` is how the out-of-band overview synthesis learns that the
  // store may have changed. It is a listener on the write path, so the two
  // properties that matter are that it fires for *every* settled write and that
  // it can never damage the write it is listening to.

  it('notifies after a successful capture', async () => {
    const { ctx, handlersOf } = makeCtx()
    const capture = vi.fn(async () => {})
    const afterPersist = vi.fn()
    registerCapture(
      { ctx, capture, afterPersist },
      { captureEnabled: () => true, nudgeEnabled: false, nudgeIntervalMs: 60_000 },
    )
    send('s1', '用户偏好黑咖啡', handlersOf)
    await new Promise(r => setTimeout(r, 10))
    expect(afterPersist).toHaveBeenCalledWith('s1', true)
  })

  it('notifies after a failed capture too', async () => {
    // A failure is followed by a rescue retry, which settles here as well — so
    // reporting only successes would still see every landed write, but a
    // failure-only notification would be indistinguishable from silence.
    const { ctx, handlersOf } = makeCtx()
    const capture = vi.fn(async () => { throw new Error('bridge down') })
    const afterPersist = vi.fn()
    registerCapture(
      { ctx, capture, afterPersist },
      { captureEnabled: () => true, nudgeEnabled: false, nudgeIntervalMs: 60_000 },
    )
    send('s1', '一条会失败的消息', handlersOf)
    await new Promise(r => setTimeout(r, 10))
    expect(afterPersist).toHaveBeenCalledWith('s1', false)
  })

  it('survives a throwing afterPersist hook', async () => {
    // A bug in the listener must not turn a successful memory write into a
    // rejected capture promise, which would mark the entry retriable and
    // re-send a message that was in fact stored.
    const { ctx, handlersOf } = makeCtx()
    const capture = vi.fn(async () => {})
    const afterPersist = vi.fn(() => { throw new Error('listener bug') })
    registerCapture(
      { ctx, capture, afterPersist },
      { captureEnabled: () => true, nudgeEnabled: false, nudgeIntervalMs: 60_000 },
    )
    send('s1', '用户偏好黑咖啡', handlersOf)
    await new Promise(r => setTimeout(r, 10))
    expect(capture).toHaveBeenCalledTimes(1)
    expect(afterPersist).toHaveBeenCalledTimes(1)
  })

  it('does not require an afterPersist hook', async () => {
    const { ctx, handlersOf } = makeCtx()
    const capture = vi.fn(async () => {})
    registerCapture(
      { ctx, capture },
      { captureEnabled: () => true, nudgeEnabled: false, nudgeIntervalMs: 60_000 },
    )
    send('s1', '用户偏好黑咖啡', handlersOf)
    await new Promise(r => setTimeout(r, 10))
    expect(capture).toHaveBeenCalledTimes(1)
  })
})
