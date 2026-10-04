/**
 * Out-of-band approval of queued topic proposals.
 *
 * ## Why this exists
 *
 * The vocabulary is registration-based: a write that proposes an unregistered
 * topic files the fact under the nearest registered ancestor and records the
 * proposal in `domain_signal` as `pending`. Only `domain_signal_promote` /
 * `domain_signal_reject` clear that row. Before this module the *only* reachable
 * promotion was a model choosing to call `memory_domains create` on its own, and
 * nothing ever asked it to — so the queue grew forever while every proposed name
 * stayed unresolved. Measured on a real store: `teaching` pending 58 times,
 * `research` 74 times, all still `pending`.
 *
 * The fix is deliberately *not* "promote everything that appears". That would
 * turn every one-off noun into permanent vocabulary. It is:
 *
 *  1. only proposals seen at least `threshold` times are candidates, and
 *  2. a model decides which of those are real topics,
 *
 * which is the same division of labour the profile synthesis uses — Python owns
 * the deterministic gate, the model owns the judgement.
 *
 * ## Why "out of band"
 *
 * The completion goes through the shared `buildLlmCompleter` seam, so it is a
 * separate call against the configured model: it never runs inside the main
 * conversation, never consumes the conversation's context, and nothing about the
 * session's prompt or turn is touched. Structurally this is the same shape as
 * `overview.ts` — note activity on a write, wait for a quiet window, then run
 * once, throttled. A write path must never wait on a model.
 *
 * ## Authority
 *
 * The model may only *approve*. It is never asked which proposals to reject, so
 * a bad judgement costs one extra topic the user can archive, rather than
 * silently discarding a name the user cares about. Rejection stays a human
 * action.
 */

import type { LlmCompleter } from './llm-extractor.ts'
import type { PythonBridge } from './bridge.ts'

/** One row of the pending registration queue, as `domain_unresolved` reports it. */
export interface PendingSignal {
  name?: string
  seen_count?: number
  nearest_ancestor?: number | null
  last_seen?: number
}

export interface DomainPromoterDeps {
  /** Bridge to the Python store. */
  bridge: PythonBridge
  /**
   * Build a completer for the approval call, or `undefined` when no model is
   * available — in which case the promoter does nothing, which is correct: the
   * queue is not lost, it simply waits for a model to exist.
   */
  completer: () => LlmCompleter | undefined
  /** Stable user scope whose vocabulary is maintained. */
  userScope: string
  /** Whether promotion runs at all, resolved at each use. */
  enabled?: () => boolean
  /** Whether the bridge is usable right now. */
  isReady?: () => boolean
  /** Idle window before a run is attempted, in ms. */
  idleMs?: number
  /** Minimum gap between two runs, in ms. `0` disables it. */
  minIntervalMs?: number
  /** Sightings a proposal needs before it is a candidate. */
  threshold?: number
  /** Cap on topics registered by one run. */
  maxPerRun?: number
  /** Logger sink. */
  log?: (message: string) => void
  /** Clock, injectable for tests. */
  now?: () => number
}

/** The promoter's public surface. */
export interface DomainPromoter {
  /** Note that a write happened. Returns immediately and never throws. */
  noteActivity: () => void
  /**
   * Run one approval pass now, bypassing the debounce but not the gate.
   *
   * Returns a short token describing what happened, so a caller can report it
   * without inferring from silence.
   */
  promoteNow: () => Promise<string>
  /** Cancel the pending timer and release resources. */
  dispose: () => void
}

/** Default idle window before an approval run. */
const DEFAULT_IDLE_MS = 60_000

/**
 * Default minimum gap between two runs.
 *
 * Deliberately longer than the idle window: the model call is the expensive
 * part, and the queue draining over a few windows costs nothing, whereas
 * back-to-back runs on a busy session would be a model call per burst.
 */
const DEFAULT_MIN_INTERVAL_MS = 30 * 60_000

/** Sightings needed before a proposal is worth a model's attention. */
const DEFAULT_THRESHOLD = 3

/**
 * Topics one run may register.
 *
 * The cap is what keeps a first run on an old store from registering hundreds of
 * names in one call: an unreviewable vocabulary, and a payload that risks the
 * completion's token budget.
 */
const DEFAULT_MAX_PER_RUN = 10

/** Token budget for the approval call. */
const PROMOTION_MAX_TOKENS = 512

/**
 * The model's instructions.
 *
 * It is asked to select from a supplied list rather than to invent names, and
 * the reply is JSON because a list of identifiers is data, not prose — the
 * overview path asks for prose and cleans it, this asks for a set and parses it.
 * The "when in doubt, include it" rule is the deliberate asymmetry from the
 * module docstring: approval is cheap and reversible, exclusion is silent.
 */
export const PROMOTION_SYSTEM = [
  'You curate a personal knowledge base\'s topic vocabulary.',
  'You are given a numbered list of proposed topic names, each with how many times it has occurred.',
  'Select the ones that name a genuine, recurring area of the user\'s work or study.',
  '',
  'Rules:',
  '- Choose ONLY from the supplied list. Never invent, rename, translate or re-spell a name.',
  '- Prefer specific names over catch-all ones: a name that could cover any work is not a topic.',
  '- Drop obvious noise: fragments, single common words, file names, timestamps, stray punctuation.',
  '- Names containing a path or URL are never topics.',
  '- When a name is plausibly a real area, include it. Excluding something real costs the user more than adding something marginal.',
  '',
  'Reply with a JSON array of the chosen names, exactly as given, e.g. ["teaching","research/paper"].',
  'Reply with [] if none qualify. No prose, no code fences.',
].join('\n')

/**
 * Render the queue as the model's input.
 *
 * Numbered and plain text: the count is what makes a recurring topic
 * distinguishable from a stray mention, and the model never sees the ids it
 * would otherwise be tempted to echo back.
 *
 * @param candidates - Queue rows that already passed the threshold.
 * @returns The prompt body, or `""` when there is nothing to ask about.
 */
export function buildPromotionPrompt(candidates: PendingSignal[]): string {
  const rows = candidates
    .map((row) => ({ name: (row.name ?? '').trim(), count: Number(row.seen_count ?? 0) }))
    .filter(row => row.name.length > 0)
  if (rows.length === 0) return ''
  const lines = rows.map((row, i) => `${i + 1}. ${row.name} (${row.count} times)`)
  return [
    `Proposed topics that are not yet in the vocabulary (${rows.length}):`,
    ...lines,
  ].join('\n')
}

/**
 * Parse the model's reply into the subset of candidate names it approved.
 *
 * The reply is intersected with the candidates, which is what stops the model
 * introducing a name that was never proposed or changing one's spelling: the
 * point of the call is a yes/no per candidate, and anything else is a malformed
 * reply rather than an instruction. A reply that is not a JSON array yields `[]`,
 * which the caller treats as "nothing to do" — the queue survives for the next
 * run, so a bad reply costs a delay rather than any data.
 *
 * The intersection is a single pass over `candidates`, not a filter of the
 * reply: iterating the candidates is what makes the result candidate-ordered
 * and duplicate-free, and it is also the only place an off-list name can be
 * rejected. A separate `Set` membership check on the reply would be equivalent
 * and therefore untestable — verified by mutation, it could be deleted with no
 * test failing.
 *
 * @param raw - The model's raw output.
 * @param candidates - The names that were offered, in order.
 * @returns The approved names, in candidate order, de-duplicated.
 */
export function parsePromotionReply(raw: string, candidates: string[]): string[] {
  if (!raw) return []
  // Models add fences unasked; strip them rather than failing the whole reply.
  const cleaned = raw.trim().replace(/^```(?:json)?\s*/i, '').replace(/\s*```$/, '')
  const start = cleaned.indexOf('[')
  const end = cleaned.lastIndexOf(']')
  if (start < 0 || end <= start) return []
  let parsed: unknown
  try {
    parsed = JSON.parse(cleaned.slice(start, end + 1))
  } catch {
    return []
  }
  if (!Array.isArray(parsed)) return []

  const approved = new Set<string>()
  for (const item of parsed) {
    if (typeof item !== 'string') continue
    approved.add(item.trim())
  }
  // Iterating the candidates (rather than the reply) is what drops an off-list
  // name, fixes the order, and removes duplicates in one pass. Candidates are
  // already canonical, so a re-cased reply name matches nothing.
  return candidates.filter(name => approved.has(name))
}

/**
 * Collapse the queue into one row per name, keeping only names that qualify.
 *
 * The queue is keyed per *scope*, not per name, so one name legitimately appears
 * several times with different counts — measured on a real store, `teaching` had
 * seven rows (`58, 15, 11, 4, 2, 2, 1`) and `tooling` nine. Two consequences make
 * this step load-bearing rather than cosmetic:
 *
 *  - **Summing is the honest count.** A name at `2, 2, 1` was proposed five
 *    times; testing each row against the threshold separately would discard a
 *    name that clearly recurs.
 *  - **De-duplication bounds the work.** One name appearing seven times would
 *    otherwise consume seven of the run's `maxPerRun` slots and produce seven
 *    identical promote calls for a single topic.
 *
 * @param rows - Raw queue rows.
 * @param threshold - Sightings, summed per name, required to qualify.
 * @returns Qualifying names with their totals, most-seen first.
 */
export function aggregateSignals(
  rows: PendingSignal[],
  threshold: number,
): Array<{ name: string; seen_count: number }> {
  const totals = new Map<string, number>()
  for (const row of rows) {
    const name = String(row.name ?? '').trim()
    if (!name) continue
    totals.set(name, (totals.get(name) ?? 0) + Number(row.seen_count ?? 0))
  }
  return [...totals.entries()]
    .filter(([, total]) => total >= threshold)
    .map(([name, seen_count]) => ({ name, seen_count }))
    // Ties broken by name so a run's selection is deterministic: without it the
    // same queue could promote a different subset on each run.
    .sort((a, b) => b.seen_count - a.seen_count || a.name.localeCompare(b.name))
}

/**
 * Create the promoter.
 *
 * @param deps - Bridge, completer factory, and the gate settings.
 * @returns The promoter's public surface.
 */
export function createDomainPromoter(deps: DomainPromoterDeps): DomainPromoter {
  const log = deps.log ?? (() => {})
  const now = deps.now ?? (() => Date.now())
  const idleMs = Math.max(0, deps.idleMs ?? DEFAULT_IDLE_MS)
  const minIntervalMs = Math.max(0, deps.minIntervalMs ?? DEFAULT_MIN_INTERVAL_MS)
  const threshold = Math.max(1, deps.threshold ?? DEFAULT_THRESHOLD)
  const maxPerRun = Math.max(1, deps.maxPerRun ?? DEFAULT_MAX_PER_RUN)

  let timer: ReturnType<typeof setTimeout> | undefined
  let disposed = false
  /** Latest completed attempt, for the minimum-gap rule. */
  let lastAttemptAt = 0
  /** The in-flight attempt, so concurrent triggers share one run. */
  let inFlight: Promise<string> | undefined

  const canRun = (): boolean => {
    if (disposed) return false
    if (deps.enabled !== undefined && !deps.enabled()) return false
    if (deps.isReady !== undefined && !deps.isReady()) return false
    return true
  }

  async function attempt(): Promise<string> {
    if (!canRun()) return 'skipped'
    // Checked before the stamp is written, so a declined run does not itself
    // reset the floor — the same ordering the overview path needed.
    if (minIntervalMs > 0 && lastAttemptAt > 0 && now() - lastAttemptAt < minIntervalMs) {
      return 'throttled'
    }

    const queue = await deps.bridge.call<PendingSignal[]>('domain_unresolved', {
      user_id: deps.userScope,
    })
    const rows = Array.isArray(queue) ? queue : []

    // The threshold is the deterministic gate; the model only ever sees what
    // survives it, so it cannot spend judgement on singletons.
    const eligible = aggregateSignals(rows, threshold)
    if (eligible.length === 0) return 'below-threshold'

    // Highest count first, so a capped run takes the topics the user recurs on
    const capped = eligible.slice(0, maxPerRun)

    const complete = deps.completer()
    if (complete === undefined) return 'no-model'

    const prompt = buildPromotionPrompt(capped)
    if (!prompt) return 'nothing-to-ask'

    const names = capped.map(row => row.name)
    const reply = await complete(PROMOTION_SYSTEM, prompt)
    const approved = parsePromotionReply(reply, names)
    if (approved.length === 0) return 'none-approved'

    let registered = 0
    let failed = 0
    for (const name of approved) {
      // Per-name isolation: an unstorable name (the local-path shapes carry a
      // colon and are rejected by design) must not abort the names after it.
      try {
        await deps.bridge.call('domain_signal_promote', {
          user_id: deps.userScope,
          name,
          display_name: '',
        })
        registered += 1
      } catch (err) {
        failed += 1
        log(`[dsh-atom-memory] topic promotion skipped "${name}": ${String(err)}`)
      }
    }
    if (registered === 0) return 'promote-failed'
    return failed > 0 ? `promoted:${registered}(failed:${failed})` : `promoted:${registered}`
  }

  function run(): Promise<string> {
    if (inFlight !== undefined) return inFlight
    // Stamped after the throttle check has had its say inside `attempt`.
    inFlight = attempt()
      .then((outcome) => {
        if (outcome !== 'skipped' && outcome !== 'throttled') lastAttemptAt = now()
        return outcome
      })
      .catch((err: unknown) => {
        // A failed promotion is not a failed session: the queue keeps the
        // proposals, and the next quiet moment tries again.
        log(`[dsh-atom-memory] topic promotion failed: ${String(err)}`)
        lastAttemptAt = now()
        return 'error'
      })
      .finally(() => {
        inFlight = undefined
      })
    return inFlight
  }

  return {
    noteActivity: () => {
      if (!canRun()) return
      if (timer !== undefined) clearTimeout(timer)
      // Debounced, not scheduled: every write pushes the deadline out, so a
      // burst produces exactly one attempt — at the pause.
      timer = setTimeout(() => {
        timer = undefined
        // Detached on purpose: `noteActivity` sits behind capture, which must
        // never wait on a model.
        void run()
      }, idleMs)
      const handle = timer as unknown as { unref?: () => void }
      handle.unref?.()
    },
    promoteNow: () => {
      if (timer !== undefined) {
        clearTimeout(timer)
        timer = undefined
      }
      return run()
    },
    dispose: () => {
      disposed = true
      if (timer !== undefined) {
        clearTimeout(timer)
        timer = undefined
      }
    },
  }
}

/** Token budget the approval call is given (exported for the caller's wiring). */
export const DOMAIN_PROMOTION_TOKENS = PROMOTION_MAX_TOKENS