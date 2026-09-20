/**
 * Durable capture hooks.
 *
 * These hooks turn conversation into memory automatically and, crucially,
 * rescue facts that would otherwise be lost:
 *
 *  1. **Per-message capture** — `user/message` durable events whose text
 *     carries a fact-worthy signal are submitted for extraction
 *     immediately. Every direct user message is captured; whether it becomes
 *     a fact is decided downstream by the LLM extractor (or rule fallback),
 *     not by a brittle keyword gate.
 *  2. **Periodic nudge** — a timer periodically re-scans recent direct user
 *     messages so facts the LLM was too busy to save are not lost. This is the
 *     *only* retry path: a pre-compression hook used to sit alongside it, but
 *     both went through the same `sweep`, so it rescued exactly the same
 *     entries and added no coverage — only a second trigger for a set the
 *     nudge already reaches.
 *
 * The per-session "recent messages" buffer is fed only from durable
 * `user/message` session events (which dsh logs and can replay), so the memory
 * written back is reproducible from the session log — it never reads live,
 * non-replayable coordination state.
 *
 * `captureEnabled` is consulted **at each hook invocation** rather than captured
 * at registration: the settings switch is meant to stop capture immediately
 * (that is the point of turning it off), and a hook that was wired at load time
 * could not honour that.
 *
 * Every dispatch is best-effort: a capture failure never throws into the loop.
 *
 * @module dsh-atom-memory/capture
 */
import type { Context } from '@deepseek-ai/cordis'
import type { Session, SessionEvent } from '@deepseek-ai/dsh-session'
import { sessionCwdOf } from './scope.ts'

interface MessageEntry {
  seq: number
  text: string
  /** True once this message has been durably captured (or surrendered to a rescue retry). */
  captured: boolean
  /** True when the immediate per-message capture failed and is therefore retriable by the nudge/compaction sweep. */
  failed: boolean
}

export interface CaptureDeps {
  ctx: Context
  /**
   * Enqueue text for extraction (LLM-first or rule fallback; caller-owned).
   *
   * @param text - The message text.
   * @param sessionId - The session it came from (provenance, and the rescue
   *   sweep's key).
   * @param cwd - The session's working directory, when the hook has the session
   *   at hand, so the write travels with the right scope signals. The sweep
   *   paths pass nothing: they only know a session id, and the caller then falls
   *   back to its own default.
   */
  capture: (text: string, sessionId: string, cwd?: string) => Promise<void>
  /** Max recent messages remembered per session. */
  maxRecent?: number
  /**
   * Notified after a capture attempt settles, successfully or not.
   *
   * The overview refresher hangs off this: a write is the only thing that can
   * make the store's overview stale, so this is the true "the store may have
   * changed" signal. It is called with the outcome rather than only on success
   * because a failed capture is followed by a rescue retry, and the retry
   * settles here too — so the refresher sees every write that actually landed
   * without this module having to model the retry.
   *
   * **Never awaited.** Capture must not wait on a summary being written, and the
   * callback is required to be non-throwing; it is wrapped anyway.
   */
  afterPersist?: (sessionId: string, ok: boolean) => void
}

export interface CaptureOptions {
  /** Whether capture runs at all, resolved at each hook invocation. */
  captureEnabled: () => boolean
  nudgeEnabled: boolean
  /** Nudge sweep period in ms. */
  nudgeIntervalMs: number
}

/** Pull the plain text out of a user message's content blocks. */
function userMessageText(event: SessionEvent): string {
  const data = event.data as { content?: Array<{ type?: string; text?: string }> }
  const blocks = data.content ?? []
  if (blocks.length === 0) return ''
  const first = blocks[0]
  return first?.type === 'text' ? (first.text ?? '') : ''
}

/** Whether a user message is a genuine human prompt (vs. plugin-sourced). */
function isDirectUserMessage(event: SessionEvent): boolean {
  const source = (event.data as { source?: { kind?: string } }).source
  return source?.kind === 'user'
}

/**
 * Register all capture hooks and return their disposers.
 */
export function registerCapture(deps: CaptureDeps, opts: CaptureOptions): (() => void)[] {
  const disposers: (() => void)[] = []
  const { ctx, capture } = deps
  const maxRecent = deps.maxRecent ?? 20
  const recent = new Map<string, MessageEntry[]>()
  const enabled = () => opts.captureEnabled() !== false

  /**
   * Fire the post-capture notification without letting it affect capture.
   *
   * Swallows everything: the callback is a listener for the overview refresher,
   * which schedules a detached task, and a bug in it must not turn a successful
   * memory write into a rejected capture promise (which would mark the entry
   * retriable and re-send it).
   */
  const notify = (sessionId: string, ok: boolean): void => {
    if (deps.afterPersist === undefined) return
    try {
      deps.afterPersist(sessionId, ok)
    } catch { /* best-effort */ }
  }

  const push = (sessionId: string, entry: MessageEntry): void => {
    const list = recent.get(sessionId) ?? []
    list.push(entry)
    while (list.length > maxRecent) list.shift()
    recent.set(sessionId, list)
  }

  /**
   * Re-scan recent messages, retrying those whose immediate capture failed.
   *
   * Only entries marked ``failed`` are retried: an entry whose immediate
   * capture is still in-flight (neither succeeded nor failed) is skipped so a
   * rescue cannot duplicate it, and an already-``captured`` one is skipped too.
   * Each entry is marked ``captured`` *before* the retry is awaited so two
   * concurrent sweeps cannot double-send the same text.
   *
   * A message that is still in flight when a sweep runs is therefore *not*
   * rescued by that sweep. It is not lost either: the in-flight capture settles
   * on its own, and only a definitive failure leaves the entry retriable for a
   * later sweep.
   */
  const sweep = async (sessionId: string): Promise<void> => {
    if (!enabled()) return
    const list = recent.get(sessionId)
    if (!list) return
    for (const entry of list) {
      if (entry.captured || !entry.failed) continue
      entry.captured = true // claim; a concurrent sweep must not resend it
      await capture(entry.text, sessionId).catch(() => { /* best-effort */ })
    }
  }

  // -- per-message capture (durable) -----------------------------------------
  disposers.push(ctx.on('session/event', (session: Session, event: SessionEvent) => {
    if (!enabled()) return
    if (event.type !== 'user/message') return
    if (!isDirectUserMessage(event)) return
    const text = userMessageText(event)
    if (text.trim().length === 0) return
    const seq = (event as { seq?: unknown }).seq as number | undefined ?? 0
    // Capture every direct user message unconditionally. Whether it holds a
    // fact worth remembering is decided downstream by the LLM extractor (or
    // the rule fallback), not by a fixed keyword list. The entry is *not*
    // marked captured up front: a failed immediate capture must be retriable
    // by the nudge / pre-compression rescue, otherwise a bridge outage would
    // silently lose the message forever. Only a successful capture (or a
    // rescue retry) marks it captured; a failure marks it `failed` so
    // `sweep` retries it.
    const entry: MessageEntry = { seq, text, captured: false, failed: false }
    push(session.id, entry)
    // The session's working directory rides along so the write carries a scope
    // context: which checkout a message came from is not derivable from its id,
    // and a harness can serve several sessions rooted in different ones.
    void capture(text, session.id, sessionCwdOf(session)).then(
      () => { entry.captured = true; notify(session.id, true) },
      () => { entry.failed = true; notify(session.id, false) },
    )
  }))

  // -- periodic nudge (timer, best-effort) -----------------------------------
  if (opts.nudgeEnabled) {
    const timer = setInterval(() => {
      // Sweep every session we've seen so far; guards against the LLM being
      // too busy to trigger capture mid-turn. Off the loop's hot path.
      if (!enabled()) return
      for (const sessionId of recent.keys()) {
        void sweep(sessionId).catch(() => { /* best-effort */ })
      }
    }, Math.max(opts.nudgeIntervalMs, 1000))
    disposers.push(() => clearInterval(timer))
  }

  return disposers
}
