/**
 * LLM-first extractor adapter.
 *
 * Extraction runs on the dsh side (where ``ctx.llm`` and the default model
 * live), then the resulting typed candidates are shipped to the Python memory
 * process via ``persist_candidates`` (RPC → ``persist_pre`` worker task). The
 * rule engine lives entirely in Python, so this adapter is the *first* path and
 * Python is the *fallback* — matching the library's LLM-first, rule-fallback
 * precedence across the process boundary.
 *
 * The default model is read from the dsh "current preset's first model"
 * selection via ``ctx.get('agentDefaultModel').currentSelection()``. When no
 * default model is available the adapter returns ``[]`` and the caller falls
 * back to the raw ``add`` path (pure Python rule extraction) — never a silent
 * drop.
 *
 * @module dsh-atom-memory/llm-extractor
 */
import { createUserMessage, BlockAssembler } from '@deepseek-ai/dsh-llm'
import type { GenerateOptions } from '@deepseek-ai/dsh-llm'
import type { Context } from '@deepseek-ai/cordis'
import type { ExtractionModelOverride } from './runtime.ts'

/**
 * One ``(key, value)`` condition: *when* a claim holds.
 *
 * Field names are the Python side's verbatim — a candidate object *is* the wire
 * payload of ``persist_candidates``, so a renamed key would be accepted by the
 * JSON parser and then silently ignored by the store.
 */
export interface ExtractedCondition {
  key: string
  value: string
}

/** One typed candidate matching the Python ``persist_candidates`` wire shape. */
export interface ExtractedCandidate {
  subject: string
  predicate: string
  object: string
  type?: string
  content?: string
  qualifiers?: Record<string, unknown>
  confidence?: number
  importance?: number
  /**
   * Conditions the claim holds *under* (language, doc_type, audience, industry,
   * stage, tool, vcs): a fact that is only true for a proposal in Chinese must
   * not outrank the same claim in the other context.
   */
  conditions?: ExtractedCondition[]
  /**
   * Free-text hint about *where* the fact belongs.
   *
   * Named exactly as the Python candidate field reads it. It is only a hint: the
   * store treats it as a low-reliability content anchor (0.25) that can
   * corroborate a resolution or accumulate in the candidate queue, but can never
   * bind a scope on its own.
   */
  scope_hint?: string
  /**
   * What the fact is *about*, as canonical names (``["teaching/ds", "programming"]``).
   *
   * Unlike ``scope_hint`` this is acted on: the store resolves each name against
   * the user's topic vocabulary, and a name the vocabulary does not hold becomes
   * its nearest registered ancestor plus a queue entry for the user to decide on.
   * The vocabulary lives in the Python store, so the prompt asks for these only
   * as plain words; the store owns what they resolve to.
   */
  domain_hints?: string[]
  /**
   * Which of ``domain_hints`` is the main topic. Moved to the front before the
   * cap is applied, so the model may list it anywhere.
   */
  primary_domain?: string
}

/** Minimal structural surface of the ``llm`` service. */
export interface LlmLike {
  stream(options: GenerateOptions): AsyncIterable<unknown>
}

/** Minimal structural surface of the default-model service. */
export interface AgentDefaultModelLike {
  currentSelection(): { provider: string; model: string; reasoningEffort?: string }
}

/** The extraction callable signature the capture/tool layer uses. */
export type ExtractFn = (text: string) => Promise<ExtractedCandidate[]>

/** Fixed, deterministic extraction prompt (strict, injection-isolated). */
const EXTRACTION_SYSTEM = `You extract atomic memory facts from a user utterance.
Return ONLY a JSON array. Each element is an object with keys:
- "subject" (entity, use "用户" for the user), "predicate" (relation),
- "object" (the value), and optionally "type", "content", "importance",
  "confidence", "conditions", "scope_hint".
"type" is one of: semantic, procedural, episodic, task, sop, decision_rule, few_shot, lesson.
For knowledge facts, put the full body in "content" and a short title in "object".

Rank every fact so the memory view can show what matters first:
- "importance" (0..1) is how durable and reusable the fact is.
- "confidence" (0..1) is how sure you are it was actually stated.

How to choose "type" - this matters, do not tag everything "semantic":
- durable rule or convention ("should/must/always", a if-then policy) -> decision_rule
- a distilled takeaway from a mistake or a hard-won finding -> lesson
- an ordered procedure or how-to that must be followed step by step -> sop
- a workflow or command sequence reported as how something is done -> procedural
- something still to be done: an open to-do, a next step, a backlog item -> task
- a stable attribute or preference of the user -> semantic
- episodic is ONLY for a dated, one-off thing that happened AND is worth
  recalling in a later session. Use it sparingly.

A to-do list is a COLLECTION, and "task" is what says so: the store treats two
facts sharing a subject and predicate as competing values *unless* the type says
they are a list, in which case the second item would silently replace the first.
So when an utterance lists several outstanding items, emit one candidate per
item, all with "type": "task", the same subject (the project the item belongs to
- use "用户" when it belongs to no project) and a short list predicate
("待办", "任务", "下一步"). Never merge several items into one "object" and never
type them "semantic".

Two optional fields say *when* and *where* a fact applies. Both are optional -
omit them rather than guessing.

"conditions" says WHEN the claim holds, as
[{"key": "<dimension>", "value": "<value>"}]. Use it only when the claim is
true of one context and not of another (a rule about Python files is not a rule
about the project). Dimensions, with example values:
- language (typescript, python), doc_type (proposal, invoice, report),
- audience (internal, customer), industry (finance, education),
- stage (draft, review, published), tool (git, excel), vcs (git, svn).
Keys must be lower_snake_case and values short and lower-case; at most a few
conditions per fact. Do NOT restate the subject or the topic as a condition.

"scope_hint" says WHERE the fact belongs - which project, client, document or
thread it came from - as a short phrase. It is a hint, not a decision: it helps
place the fact, it never overrides where the session actually is. Use it only
when the text names a place the fact belongs to; never invent one.
For a fact that is true of the user themselves rather than of the current piece
of work - a durable preference, a stable attribute ("likes coffee", "prefers
concise answers", "writes in Chinese") - put the exact marker "user" here
instead of a place name, or omit the field. Do not name the current project for
such a fact: a preference captured while working on one chapter holds for every
chapter, and naming the project would hide it from the others.

"domain_hints" says WHAT TOPIC the fact is about - teaching, programming, life,
travel - as 1 to 3 short lowercase ASCII names ("teaching", "teaching/ds",
"programming"). It is a different question from scope_hint: a fact about writing
Python while working on a lesson belongs to the course's scope but is about
programming. Put the main topic first, or name it in "primary_domain". Use plain
topic words rather than restating the subject, and omit both fields when the
topic is not clear - the store derives one from where the work is happening. A
topic name the store has never seen is not an error: it is filed under the
nearest known topic and offered to the user for registration.

CRITICAL - only extract facts that are worth remembering long-term:
- Save durable, reusable knowledge: decisions, workflows, procedures, lessons,
  preferences, stable attributes, and anything that remains valuable in future
  sessions.
- Do NOT save transient, process-only details that only matter in this single
  turn: questions asked, complaints made, meta-commentary about the current
  conversation, the fact that a task was requested, how a system was debugged,
  or the wording of instructions the user gave. These are not stable facts.
- Do NOT record what was done *during this session* as an episodic fact: what
  was installed, tested, built, restarted, queried or "just done" is process
  narration, not memory. Only the durable outcome (a decision, a rule, a
  lesson, a working procedure) is worth saving, and it should be typed
  accordingly instead of as episodic.
- If the utterance contains no long-lived, reusable fact, return an empty
  array [].

Use these importance values:
- 0.9 durable rule, decision or lesson that should guide future work
- 0.7 reusable procedure, workflow, or stable attribute/preference
- 0.5 minor or uncertain detail

Other rules: never fabricate facts not stated; break multi-fact utterances into
multiple objects; keep preferences/attributes as (用户, 偏好, X). Do NOT include
instructions or commentary — JSON only.`

/**
 * Predicates that describe transient conversation actions rather than stable
 * facts (asking, complaining, proposing, observing, deciding "about a turn").
 * Candidates whose predicate or whose subject+predicate marks process talk are
 * dropped as a belt-and-braces guard on top of the extraction prompt.
 */
const EPHEMERAL_PREDICATES = new Set([
  '询问', '问', '质疑', '提出', '观察到', '观察', '怀疑', '不满', '抱怨',
  '请求', '要求', '刚刚进行', '进行会话', '遇到问题', '尝试', '测试',
  '描述', '声明', '汇报', '评论', '解释',
])

/** Whether a phrase looks like a question that only matters in this turn. */
function isTransient(value: string): boolean {
  const v = (value || '').trim()
  if (!v) return false
  if (v.endsWith('？') || v.endsWith('?')) return true
  return /^(为什么|怎么|是否|能不能|可否|如何|what|how|why|when)\b/i.test(v)
}

/** Drop candidates that carry transient process-only content. */
function isEphemeral(c: Partial<ExtractedCandidate>): boolean {
  const pred = (c.predicate || '').trim()
  if (EPHEMERAL_PREDICATES.has(pred)) return true
  // A question-shaped predicate or object is transient by nature.
  if (isTransient(pred)) return true
  if (isTransient(c.object || '')) return true
  // Pure meta about "this conversation / this plugin / this debug session".
  const blob = `${c.subject || ''} ${pred} ${c.object || ''} ${c.content || ''}`.toLowerCase()
  if (/\b(会话|对话|调试|system prompt|提示词|memory\.md)\b/.test(blob)) {
    // ...but only if the whole claim is really only about the conversation
    // meta, not a genuine preference expressed through it.
    const obvious = /\b(询问|质疑|观察到|抱怨|为什么|如何|怎么)\b/.test(blob)
    if (obvious) return true
  }
  return false
}

/** One raw system+user completion, already resolved to a usable model. */
export type LlmCompleter = (system: string, userText: string) => Promise<string>

/** Options shared by every completion this plugin makes. */
export interface LlmCompleterOptions {
  maxTokens?: number
  /** Manual provider/model override; wins over the dsh default selection. */
  modelOverride?: () => ExtractionModelOverride | undefined
  /** When it returns false the completion is not attempted at all. */
  enabled?: () => boolean
  /** Inject a fetch for the direct-endpoint path (testability). Defaults to global fetch. */
  fetchImpl?: (input: string, init?: Record<string, unknown>) => Promise<{ ok: boolean; body: { getReader(): unknown } }>
  /** Inject a logger sink (defaults to ctx.logger). */
  log?: (message: string) => void
  /** Label used in the truncation warning, e.g. "extraction". */
  label?: string
}

/**
 * Resolve a model and build a raw text completer over the dsh `llm` service.
 *
 * This is the seam both LLM callers in this plugin share — fact extraction and
 * profile synthesis. It exists as its own factory because model resolution is
 * not trivial (manual override, else the dsh default selection, else nothing),
 * a custom OpenAI-compatible endpoint has to bypass `ctx.llm` entirely, and the
 * truncation check below is what stops a half-written JSON payload from being
 * parsed as if it were complete. A second hand-written copy of all that in the
 * profile path would be a second place for the API key handling and the timeout
 * to drift.
 *
 * @returns ``undefined`` when no `llm` service and no usable model is
 *   available, so callers can degrade cleanly instead of failing at call time.
 */
export function buildLlmCompleter(
  ctx: Context,
  opts: LlmCompleterOptions = {},
): LlmCompleter | undefined {
  const llm = ctx.get('llm') as LlmLike | undefined
  const modelOverride = opts.modelOverride?.()
  const log = opts.log ?? ((m: string) => { ctx.logger?.(m) })

  const def = ctx.get('agentDefaultModel') as AgentDefaultModelLike | undefined

  let provider = modelOverride?.provider?.trim() ?? ''
  let model = modelOverride?.model?.trim() ?? ''
  if (!provider && def !== undefined) {
    try {
      const selection = def.currentSelection()
      if (selection !== undefined) {
        provider = selection.provider
        model = selection.model
      }
    } catch {
      /* ignore; fall through */
    }
  }
  if (!provider || !model) return undefined

  // Custom OpenAI-compatible endpoint: when the override names a baseURL we
  // call it directly; the call then works even for endpoints dsh has no
  // provider adapter for.
  const baseURL = modelOverride?.baseURL?.trim() ?? ''
  const apiKey = modelOverride?.apiKey ?? ''
  const hasCustomEndpoint = baseURL.length > 0

  // The `llm` service is required only for the ctx.llm path; a custom endpoint
  // is called over plain fetch and needs no dsh LLM provider.
  if (!hasCustomEndpoint && llm === undefined) return undefined

  const enabled = opts.enabled
  const maxTokens = opts.maxTokens ?? 2048
  const fetchImpl = opts.fetchImpl
  const label = opts.label ?? 'extraction'

  return async (system: string, userText: string): Promise<string> => {
    if (enabled?.() === false) return ''
    if (hasCustomEndpoint) {
      return await extractViaEndpoint({
        baseURL,
        model,
        apiKey,
        system,
        userText,
        maxTokens,
        fetchImpl,
        log,
      })
    }
    const messages = [
      createUserMessage({
        content: [{ type: 'text', text: userText }],
        source: { kind: 'plugin', plugin: 'dsh-atom-memory' } as never,
      }),
    ]
    const options: GenerateOptions = {
      provider,
      model,
      messages: messages as never[],
      system,
      maxTokens,
      purpose: 'session-title',
    }
    const assembler = new BlockAssembler()
    // `llm` is guaranteed present here: the build-time guard above returns
    // undefined when there is no custom endpoint and no `llm` service.
    for await (const chunk of llm!.stream(options)) {
      assembler.push(chunk as never)
    }
    const finished = assembler.finish
    if (finished.kind !== 'stop') {
      // Truncated (or otherwise unfinished) output: partial JSON is unusable,
      // so it is discarded and the caller degrades. Log it, because a budget
      // that is too small otherwise loses a long result invisibly.
      log(
        `[atom-memory] ${label} not persisted (finish=${finished.kind}); `
        + `consider raising the token budget (now ${maxTokens})`,
      )
      return ''
    }
    return assembler.blocks()
      .filter(b => b.type === 'text')
      .map(b => (b as { text?: string }).text ?? '')
      .join('')
      .trim()
  }
}

/**
 * Build the LLM-first extraction function bound to the dsh `llm` service and
 * the configured model.
 *
 * Model resolution: a manual ``extractionModel`` override wins when it names a
 * provider, otherwise the dsh current-preset default selection is used. When
 * neither yields a usable provider/model, ``undefined`` is returned and the
 * caller falls back to the Python rule engine (never a silent drop).
 *
 * Custom endpoint: when the override also names a ``baseURL`` (API 地址), the
 * extractor calls that OpenAI-compatible endpoint directly
 * (``POST {baseURL}/chat/completions``, ``Authorization: Bearer {apiKey}``, SSE)
 * instead of routing through ``ctx.llm``. The API key travels only in the
 * Authorization header and is never logged. Protocol is assumed `openai`.
 *
 * @returns ``undefined`` when no `llm` service and no usable model is
 *   available, so callers can disable the LLM path cleanly.
 */
export function buildLlmExtractor(
  ctx: Context,
  opts: {
    maxTokens?: number
    /** Manual provider/model override; wins over the dsh default selection. */
    modelOverride?: () => ExtractionModelOverride | undefined
    /** When it returns false the extractor yields nothing (caller falls back). */
    enabled?: () => boolean
    /** Inject a fetch for the direct-endpoint path (testability). Defaults to global fetch. */
    fetchImpl?: (input: string, init?: Record<string, unknown>) => Promise<{ ok: boolean; body: { getReader(): unknown } }>
    /** Inject a logger sink (defaults to ctx.logger). */
    log?: (message: string) => void
  } = {},
): ExtractFn | undefined {
  const complete = buildLlmCompleter(ctx, { ...opts, label: 'extraction' })
  if (complete === undefined) return undefined

  return async (text: string): Promise<ExtractedCandidate[]> => {
    const raw = await complete(EXTRACTION_SYSTEM, text)
    if (!raw) return []
    // Strip accidental code fences defensively.
    const cleaned = raw.replace(/^```(?:json)?\s*/i, '').replace(/\s*```$/, '')
    return parseCandidates(cleaned)
  }
}

/**
 * One OpenAI-compatible streaming completion over a custom endpoint. Strips the
 * JSON payload to the finished text, throwing on a transport/HTTP error so the
 * caller can fall back. The API key goes only in the Authorization header and
 * is never logged.
 *
 * @param deps.fetchImpl - injected fetch (testability); the global fetch when
 *   omitted.
 */
export async function extractViaEndpoint(deps: {
  baseURL: string
  model: string
  apiKey: string
  system: string
  userText: string
  maxTokens: number
  fetchImpl?: (input: string, init?: Record<string, unknown>) => Promise<{ ok: boolean; body: { getReader(): unknown } }>
  log: (message: string) => void
  /** Max wall-clock time for the whole request, ms. Aborts on exceed. */
  timeoutMs?: number
}): Promise<string> {
  const {
    baseURL, model, apiKey, system, userText, maxTokens, log, timeoutMs = 60_000,
  } = deps
  const fetchImpl = deps.fetchImpl ?? (globalThis as { fetch?: unknown }).fetch as
    ((input: string, init?: Record<string, unknown>) => Promise<{ ok: boolean; body: { getReader(): unknown } }>)
  if (typeof fetchImpl !== 'function') {
    throw new Error('custom extraction endpoint requires a fetch implementation')
  }
  const url = `${baseURL.replace(/\/+$/u, '')}/chat/completions`
  log(`[atom-memory] extraction via custom endpoint ${baseURL} model=${model}`)
  // Bound the request: a hung endpoint must not stall the extraction path
  // (the per-message capture, the pre-compression sweep and the `memory_add`
  // tool all await it, and the RPC timeout does not cover this direct call).
  const controller = new AbortController()
  const timer = setTimeout(() => controller.abort(), timeoutMs)
  const cleanup = (): void => clearTimeout(timer)
  try {
    const response = await fetchImpl(url, {
      method: 'POST',
      headers: {
        'Content-Type': 'application/json',
        Accept: 'text/event-stream',
        ...(apiKey ? { Authorization: `Bearer ${apiKey}` } : {}),
      },
      body: JSON.stringify({
        model,
        messages: [
          { role: 'system', content: system },
          { role: 'user', content: userText },
        ],
        stream: true,
        max_tokens: maxTokens,
      }),
      // TS: the injected fetch impl's signature is narrowed; forward the signal
      // as an unknown field so a real fetch aborts the body read on timeout.
      signal: controller.signal,
    } as Record<string, unknown>)
    if (!response.ok) {
      throw new Error(`custom endpoint ${baseURL} returned HTTP ${(response as { status?: unknown }).status ?? 'error'}`)
    }
    return await collectSseText(response.body as never as { getReader(): { read(): Promise<{ done: boolean; value?: Uint8Array }> } })
  } catch (err) {
    if (controller.signal.aborted) {
      throw new Error(`custom endpoint ${baseURL} timed out after ${timeoutMs}ms`)
    }
    throw err
  } finally {
    cleanup()
  }
}

/**
 * Read an SSE response body, concatenating OpenAI `choices[].delta.content`
 * until `[DONE]`. Returns the full text; strips an SSE `data:` prefix per line.
 */
export async function collectSseText(
  body: { getReader(): { read(): Promise<{ done: boolean; value?: Uint8Array }> } },
): Promise<string> {
  const reader = body.getReader()
  const decoder = new TextDecoder()
  let buffer = ''
  let out = ''
  for (;;) {
    const { done, value } = await reader.read()
    if (done) break
    buffer += decoder.decode(value, { stream: true })
    let nl: number
    while ((nl = buffer.indexOf('\n')) !== -1) {
      const line = buffer.slice(0, nl).trim()
      buffer = buffer.slice(nl + 1)
      if (line.startsWith('data:')) {
        const payload = line.slice(5).trim()
        if (payload === '[DONE]') return out
        if (!payload) continue
        try {
          const event = JSON.parse(payload) as { choices?: Array<{ delta?: { content?: string } }> }
          const delta = event.choices?.[0]?.delta?.content
          if (delta) out += delta
        } catch {
          /* skip a malformed SSE data line */
        }
      }
    }
  }
  return out
}

/**
 * Coerce a model-supplied score into a usable 0..1 number.
 *
 * Models routinely return scores as strings (`"0.9"`) or out of range; both
 * would otherwise be dropped and the fact would fall back to the neutral
 * default, tying it with every other fact and hiding it from the ordered view.
 *
 * @param value - The raw field value.
 * @returns A clamped score, or `undefined` when nothing usable was supplied.
 */
function parseScore(value: unknown): number | undefined {
  if (typeof value === 'number') {
    return Number.isFinite(value) ? clamp01(value) : undefined
  }
  if (typeof value === 'string') {
    const parsed = Number.parseFloat(value.trim())
    return Number.isFinite(parsed) ? clamp01(parsed) : undefined
  }
  return undefined
}

/** Clamp a number into the inclusive 0..1 range. */
function clamp01(value: number): number {
  return value < 0 ? 0 : value > 1 ? 1 : value
}

/**
 * Coerce the model's `conditions` field into usable `(key, value)` pairs.
 *
 * Tolerant in the same way as the numeric parsing: a malformed entry is dropped
 * rather than failing the whole candidate (a fact without conditions is still a
 * fact), and the mapping form (`{"language": "typescript"}`) is understood as
 * well as the list the prompt asks for — a model that answers with the other
 * spelling has still answered. Values are kept as written; the store normalises
 * and caps them.
 *
 * @param value - The raw field value.
 * @returns The usable conditions, or `undefined` when none were supplied.
 */
function parseConditions(value: unknown): ExtractedCondition[] | undefined {
  const out: ExtractedCondition[] = []
  const push = (key: unknown, val: unknown): void => {
    if (typeof key !== 'string' || typeof val !== 'string') return
    const cleanKey = key.trim()
    const cleanValue = val.trim()
    if (cleanKey.length === 0 || cleanValue.length === 0) return
    out.push({ key: cleanKey, value: cleanValue })
  }
  if (Array.isArray(value)) {
    for (const item of value) {
      if (typeof item !== 'object' || item === null) continue
      const entry = item as { key?: unknown; value?: unknown }
      push(entry.key, entry.value)
    }
  } else if (typeof value === 'object' && value !== null) {
    for (const [key, val] of Object.entries(value as Record<string, unknown>)) push(key, val)
  }
  return out.length > 0 ? out : undefined
}

/**
 * Coerce the model's `scope_hint` into a usable hint.
 *
 * @param value - The raw field value.
 * @returns The trimmed hint, or `undefined` when nothing usable was supplied.
 */
function parseScopeHint(value: unknown): string | undefined {
  if (typeof value !== 'string') return undefined
  const trimmed = value.trim()
  return trimmed.length > 0 ? trimmed : undefined
}

/** Most topic proposals one candidate may carry (the store caps again). */
const MAX_DOMAIN_HINTS = 3

/**
 * Coerce the model's topic proposals into a usable list.
 *
 * The names are *not* validated into a vocabulary here: the vocabulary lives in
 * the Python store, which resolves an unknown name to its nearest registered
 * ancestor. This only drops what cannot be a name at all (non-strings, blanks)
 * and de-duplicates, so a chatty model cannot make one fact carry a paragraph.
 *
 * @param value - The raw `domain_hints` field (a list, or a single string).
 * @returns The proposals, or `undefined` when there are none.
 */
function parseDomainHints(value: unknown): string[] | undefined {
  const raw = typeof value === 'string' ? [value] : value
  if (!Array.isArray(raw)) return undefined
  const out: string[] = []
  for (const item of raw) {
    if (typeof item !== 'string') continue
    const name = item.trim()
    if (name.length === 0 || out.includes(name)) continue
    out.push(name)
    if (out.length >= MAX_DOMAIN_HINTS) break
  }
  return out.length > 0 ? out : undefined
}

/**
 * Keep a `primary_domain` only when it is one of the proposals.
 *
 * A primary the store cannot see is worse than none: it would silently become
 * whichever label the resolver happened to put first.
 *
 * @param value - The raw `primary_domain` field.
 * @param hints - The proposals it must belong to.
 * @returns The matching proposal, or `undefined`.
 */
function parsePrimaryDomain(value: unknown, hints: string[] | undefined): string | undefined {
  if (typeof value !== 'string' || !hints || hints.length === 0) return undefined
  const name = value.trim()
  return name.length > 0 && hints.includes(name) ? name : undefined
}

/**
 * Parse and sanitize the LLM's JSON output into typed candidates. Malformed or
 * non-object entries are dropped; a fully-invalid payload yields ``[]`` so the
 * caller can fall back to rules.
 */
export function parseCandidates(raw: string): ExtractedCandidate[] {
  // Defensively strip accidental code fences before parsing.
  const cleaned = raw.trim().replace(/^```(?:json)?\s*/i, '').replace(/\s*```$/, '')
  let parsed: unknown
  try {
    parsed = JSON.parse(cleaned)
  } catch {
    return []
  }
  if (!Array.isArray(parsed)) return []
  const out: ExtractedCandidate[] = []
  for (const item of parsed) {
    if (typeof item !== 'object' || item === null) continue
    const c = item as Partial<ExtractedCandidate>
    if (typeof c.subject !== 'string' || typeof c.predicate !== 'string' || typeof c.object !== 'string') {
      continue
    }
    // Belt-and-braces: drop transient process-only candidates the prompt
    // may have let through (asking/complaining/proposing this-turn talk).
    if (isEphemeral(c)) continue
    const domainHints = parseDomainHints(c.domain_hints)
    out.push({
      subject: c.subject,
      predicate: c.predicate,
      object: c.object,
      type: typeof c.type === 'string' ? c.type : undefined,
      content: typeof c.content === 'string' ? c.content : undefined,
      qualifiers: c.qualifiers,
      confidence: parseScore(c.confidence),
      importance: parseScore(c.importance),
      conditions: parseConditions(c.conditions),
      scope_hint: parseScopeHint(c.scope_hint),
      domain_hints: domainHints,
      primary_domain: parsePrimaryDomain(c.primary_domain, domainHints),
    })
  }
  return out
}

