/**
 * Explicit memory tools the model can call (design §4.3 / dsh memory surface).
 *
 * Each ``execute`` is a thin, structured delegation to the Python bridge.
 * ``memory_recall`` forwards the query and the store does retrieval; the model
 * never reasons about atomic facts itself. ``memory_add`` is LLM-first: it runs
 * the shared in-process extractor and ships typed candidates (falling back to
 * the Python rule path when the LLM path is unavailable or yields nothing), so
 * free-form content is not silently dropped by the rule engine's narrow
 * patterns.
 *
 * **A write reports what happened.** The store decides conflicts itself — under
 * a single-valued predicate a newer assertion replaces the stored value, and a
 * contradiction from weaker evidence is refused — so a tool that only said
 * "queued" would hide the decision that matters most. ``memory_add`` /
 * ``memory_replace`` / ``memory_forget`` therefore ask the bridge for the
 * outcome (a bounded wait, not a synchronous pipeline) and render it: written,
 * replaced, or refused-with-reason. ``memory_snapshot`` closes the other half of
 * the loop by showing the exact text the session's prompt carries.
 *
 * **Every call travels with its context.** Reads and writes carry the session's
 * ``scope_context`` (see `scope.ts`) because the store's answer depends on
 * *where* the work happens, not only on what was asked. ``memory_scope`` is the
 * management surface for that hierarchy: what this context resolves to, which
 * hypotheses are still waiting for evidence, and how to confirm, alias, create
 * or merge a scope. A deployment with no context sends nothing at all and
 * behaves exactly as it did before scope awareness existed.
 *
 * Tools are the only model-visible surface. What the model actually reads is
 * the ``ContentBlock[]`` returned by ``output.render``; ``output.schema`` only
 * types/validates the structured value (and tags what a host presenter may
 * project). A fact that is not spelled out in ``render`` is therefore invisible
 * to the model no matter what the structured value carries.
 *
 * @module dsh-atom-memory/tools
 */
import type { Context } from '@deepseek-ai/cordis'
import { defineTool } from '@deepseek-ai/dsh-tools'
import type { ToolRunContext } from '@deepseek-ai/dsh-tools'
import type { PythonBridge } from './bridge.ts'
import type { ExtractFn, ExtractedCandidate } from './llm-extractor.ts'
import type { FrozenSnapshotHandle } from './context.ts'
import type { ScopeContextPayload, SessionCwdSource } from './scope.ts'

/**
 * Minimum trimmed length (characters) for the raw knowledge fallback. Below
 * this a `memory_add` payload is treated as an ordinary short utterance and
 * routed to the rule engine instead.
 */
const RAW_KNOWLEDGE_MIN_CHARS = 120

/** Predicate stamped on raw-fallback knowledge facts. */
const RAW_KNOWLEDGE_PREDICATE = '知识'

/** Longest title kept from the first line of a raw-fallback body. */
const RAW_KNOWLEDGE_TITLE_CHARS = 60

/** Importance floor for a fact the user *explicitly* asked to remember. */
const EXPLICIT_IMPORTANCE = 0.9

/** Confidence stamped on a fact the user explicitly asked to remember. */
const EXPLICIT_CONFIDENCE = 0.9

/** How the store may answer a write. Mirrors the Python write outcome. */
interface WriteReceipt {
  candidate_id?: string
  status?: string
  reject_kind?: string
  reject_reason?: string
  outcome?: {
    written?: string[]
    superseded?: Array<{
      old_object?: string | null
      new_object?: string | null
      predicate?: string
      reason?: string
    }>
    rejected?: Array<{
      kind?: string
      reason?: string
      detail?: string
      predicate?: string
      object?: string
      stored_object?: string
    }>
    reinforced?: Array<{
      fact_id?: string
      object?: string
      applied?: boolean
      /** ``fingerprint`` (identical content) or ``embedding`` (reworded body). */
      on?: string
    }>
    truncated?: Array<{ field?: string; original_chars?: number; kept_chars?: number }>
    retracted?: string[]
    purged?: string[]
  }
}

/**
 * Stamp explicit-remember priority onto extracted candidates.
 *
 * A `memory_add` call is the user saying "keep this", which is the strongest
 * durability signal available, so it sets a floor on `importance` — the value
 * that decides where the fact lands in the priority-ordered memory view.
 * Extraction may still rank a candidate *higher* (a long SOP body it judged
 * critical); it is never lowered.
 *
 * @param candidates - Candidates produced by the extractor.
 * @returns The same candidates with the explicit-remember floor applied.
 */
export function stampExplicitPriority(
  candidates: ExtractedCandidate[],
): ExtractedCandidate[] {
  return candidates.map(c => ({
    ...c,
    importance: Math.max(c.importance ?? 0, EXPLICIT_IMPORTANCE),
    confidence: Math.max(c.confidence ?? 0, EXPLICIT_CONFIDENCE),
  }))
}

/**
 * Build a candidate that stores a payload verbatim as long-form knowledge.
 *
 * Used only when the caller explicitly asked to remember the content and
 * extraction produced nothing usable. ``type`` is long-form knowledge so the
 * body stays out of the summary digest (which advertises it by ``fact_id``
 * instead of inlining it), and the explicit-remember priority applies because
 * the user asked for this specific content to be kept.
 *
 * @param text - The trimmed content to store.
 * @returns A candidate carrying the full body in ``content``.
 */
export function rawKnowledgeCandidate(text: string): ExtractedCandidate {
  const body = text.trim()
  const firstLine =
    body.split(/\r?\n/).map(l => l.trim()).find(l => l.length > 0) ?? body
  const title =
    firstLine.length <= RAW_KNOWLEDGE_TITLE_CHARS
      ? firstLine
      : `${firstLine.slice(0, RAW_KNOWLEDGE_TITLE_CHARS)}…`
  return {
    subject: '用户',
    predicate: RAW_KNOWLEDGE_PREDICATE,
    object: title,
    type: 'sop',
    content: body,
    importance: EXPLICIT_IMPORTANCE,
    confidence: EXPLICIT_CONFIDENCE,
  }
}

/**
 * Render a write receipt for the model.
 *
 * The point of this function is that a refusal is *legible*: "written" and
 * "refused because a better-evidenced claim is already stored" are different
 * facts about the world, and a memory tool that reports both as success teaches
 * the model to trust a store that is not there.
 *
 * @param receipt - The bridge's reply to a write call.
 * @returns One line describing what the store did.
 */
export function renderWriteReceipt(receipt: WriteReceipt): string {
  const outcome = receipt.outcome ?? {}
  const written = outcome.written ?? []
  const superseded = outcome.superseded ?? []
  const rejected = outcome.rejected ?? []
  const reinforced = outcome.reinforced ?? []
  const truncated = outcome.truncated ?? []
  const retracted = outcome.retracted ?? []
  const purged = outcome.purged ?? []

  // Shortening is reported first and unconditionally: it is the one outcome the
  // caller cannot discover afterwards, because the stored text is already the
  // shortened one.
  const shortened = truncated
    .map(t => `${t.field ?? '内容'}（保留前 ${t.kept_chars ?? '?'} 字符，原 ${t.original_chars ?? '?'}）`)
    .join('，')

  if (receipt.status === 'pending') {
    const head = '已入队（尚未落库）。稍后可用 memory_snapshot 或 memory_summary_detail 确认结果。'
    return shortened ? `${head}\n注意：内容过长已截断——${shortened}` : head
  }
  if (receipt.status === 'error') {
    return `写入失败：${receipt.reject_reason ?? '存储侧报错'}（未写入任何记忆）`
  }
  if (purged.length > 0) return `已彻底删除 ${purged.length} 条记忆（不可恢复）。`
  if (retracted.length > 0) return `已遗忘 ${retracted.length} 条记忆（软删除）。`

  const parts: string[] = []
  if (written.length > 0) parts.push(`已记住 ${written.length} 条`)
  if (superseded.length > 0) {
    const detail = superseded
      .map(s => `${s.predicate ?? '?'}: ${s.old_object ?? '?'} → ${s.new_object ?? '(替换)'}`)
      .join('；')
    parts.push(`并替换了 ${superseded.length} 条旧值（${detail}）`)
  }
  if (reinforced.length > 0) {
    // A repeat is not a write. Saying so explicitly is what stops the model from
    // believing it added a memory it did not add — and the `on` field tells a
    // human which test fired: `embedding` means the bodies were an approximate
    // match, anything else (`idempotent`, `fingerprint`) means identical content.
    const semantic = reinforced.filter(r => (r.on ?? '') === 'embedding').length
    const exact = reinforced.length - semantic
    const how = [
      exact > 0 ? `${exact} 条内容完全相同` : '',
      semantic > 0 ? `${semantic} 条语义近似` : '',
    ].filter(Boolean).join('、')
    parts.push(`其中 ${reinforced.length} 条与已存记忆重复，已合并为复用确认（${how}）`)
  }
  if (rejected.length > 0) {
    const first = rejected[0]!
    const reason =
      first.detail
      || first.reason
      || (first.kind === 'conflict' ? '与已存记忆冲突' : String(first.kind ?? '被拒绝'))
    parts.push(`拒绝 ${rejected.length} 条：${reason}`)
  }
  if (shortened) parts.push(`注意：内容过长已截断——${shortened}`)
  if (parts.length === 0) return '没有可写入的事实（抽取为空）。'
  return `${parts.join('，')}。`
}

/** Ask the store for the outcome of a write, within a bounded wait. */
function writeParams(
  deps: { writeAckTimeoutMs?: number },
  extra: Record<string, unknown>,
): Record<string, unknown> {
  return { wait_ms: deps.writeAckTimeoutMs ?? 0, ...extra }
}

export interface ToolDeps {
  ctx: Context
  bridge: PythonBridge
  fallbackScope: string
  maxRecalledFacts: number
  summaryTokens: number
  /**
   * LLM-first extractor (dsh default model). When present, ``memory_add``
   * runs extraction in-process and ships typed candidates to the Python side
   * via ``persist_candidates``; the raw ``add`` rule path is the fallback.
   * Absent means the rule engine is the only extractor (original behaviour).
   */
  extract?: ExtractFn
  /** Master-switch gate: when it returns false every tool rejects with a clear error. */
  isEnabled?: () => boolean
  /**
   * Token budget for the compact digest, resolved per call so the tool renders
   * at the *same* budget the session prompt freezes at. Absent falls back to
   * ``summaryTokens``.
   */
  resolveSummaryBudget?: () => number
  /**
   * How long a write tool waits for the store's verdict before returning the
   * enqueue receipt. ``0``/absent never waits.
   */
  writeAckTimeoutMs?: number
  /** Frozen-snapshot handle, used by ``memory_snapshot`` for the audit view. */
  snapshot?: FrozenSnapshotHandle
  /**
   * Build the `scope_context` payload for one call site, so every read and
   * write travels with the context it happened in (which project, which
   * document, which phase).
   *
   * Optional: absent means a scope-blind deployment, and then no call carries
   * `scope_context` at all — the params stay exactly what they were before
   * scope awareness existed. Either way an `undefined` return wins: a context
   * with nothing in it is not sent.
   */
  scopeContext?: (source?: SessionCwdSource) => ScopeContextPayload | undefined
}

/** Thrown when the memory master switch is off. */
function disabledError(): Error {
  return new Error('memory is disabled')
}

/**
 * The optional `scope_context` RPC parameter for one call site.
 *
 * Spread into a params object. An absent payload contributes **nothing** — not
 * an empty object — so a scope-blind deployment (or a session whose context
 * yielded no evidence) keeps sending exactly the params it sent before scope
 * awareness existed, and the store sides with the global scope as it always did.
 *
 * @param deps - Tool dependencies, whose `scopeContext` builder is optional.
 * @param source - The call site's session source (a tool run).
 * @returns `{scope_context}` or an empty object.
 */
function scopeParam(deps: ToolDeps, source?: SessionCwdSource): Record<string, unknown> {
  const payload = deps.scopeContext?.(source)
  return payload === undefined ? {} : { scope_context: payload }
}

/**
 * One row of the scope hierarchy, as ``scope_list`` returns it.
 *
 * The scope surface is an operator/model *management* view, so its shapes are
 * declared here rather than imported: they are the Python store's dicts, and
 * every field is optional because an older store may omit one.
 */
interface ScopeRow {
  scope_id?: number
  scope_type?: string
  name?: string
  display_name?: string
  parent_id?: number | null
  path?: string
  status?: string
  confidence?: number
}

/** One scope hypothesis that has not earned creation yet. */
interface ScopeCandidate {
  scope_type?: string
  name?: string
  parent_id?: number | null
  signal_type?: string
  signal_value?: string
  confidence?: number
  seen_count?: number
}

/** What a context resolved to, plus the evidence behind the decision. */
interface ScopeResolution {
  scope_id?: number
  scope_type?: string | null
  path?: string | null
  display_name?: string | null
  confidence?: number
  status?: string
  conditions?: Array<{ key?: string; value?: string }>
  candidates?: ScopeCandidate[]
  matched?: string[]
  detail?: string
}

/** Format a confidence for the model (two decimals, never `NaN`). */
function fmtConfidence(value: number | undefined): string {
  return typeof value === 'number' && Number.isFinite(value) ? value.toFixed(2) : '?'
}

/** Render one scope as a single line, prefixed by how it was obtained. */
function scopeLine(row: ScopeRow, verb: string): string {
  const place = row.path ?? row.name ?? '?'
  const meta = [
    row.scope_type ?? '?',
    `状态 ${row.status ?? 'active'}`,
    `置信度 ${fmtConfidence(row.confidence)}`,
  ].join(' · ')
  return `${verb} [${row.scope_id ?? '?'}] ${place}（${meta}）`
}

/** Render the scope tree, indented by each scope's depth in its path. */
function renderScopeList(value: { scopes?: ScopeRow[] }): string {
  const scopes = value.scopes ?? []
  if (scopes.length === 0) return '（没有作用域）'
  const lines = scopes.map((s) => {
    const place = s.path ?? s.name ?? '?'
    const depth = Math.max(0, place.split('/').filter(part => part.length > 0).length - 1)
    return `${'  '.repeat(depth)}${scopeLine(s, '-')}`
  })
  return [`作用域树（${scopes.length} 个，按路径缩进）：`, ...lines].join('\n')
}

/**
 * Render what the current context resolves to.
 *
 * The unresolved candidate queue is part of the answer, not a footnote: "this
 * session is not in any scope yet" and "this session keeps looking like project
 * X but has not proven it" are different states, and only the queue tells them
 * apart — which is also what the model needs in order to decide whether to
 * `create` the scope explicitly.
 */
function renderScopeResolve(value: { resolution?: ScopeResolution; candidates?: ScopeCandidate[] }): string {
  const r = value.resolution ?? {}
  const place = r.scope_type === 'global'
    ? '全局作用域（没有更具体的匹配）'
    : `${r.path ?? '?'}（${r.scope_type ?? '?'}）`
  const lines = [
    `当前上下文 → [${r.scope_id ?? '?'}] ${place}`,
    `置信度 ${fmtConfidence(r.confidence)} · 状态 ${r.status ?? '?'}`,
  ]
  if ((r.matched ?? []).length > 0) lines.push(`匹配信号：${(r.matched ?? []).join(' / ')}`)
  const conditions = (r.conditions ?? [])
    .map(c => `${c.key ?? '?'}=${c.value ?? '?'}`)
    .join('，')
  if (conditions) lines.push(`条件：${conditions}`)
  if (r.detail) lines.push(`说明：${r.detail}`)
  if ((r.candidates ?? []).length > 0) {
    lines.push(`本次解析记下候选 ${(r.candidates ?? []).length} 条（证据不足，尚未建档）`)
  }

  const queue = value.candidates ?? []
  if (queue.length === 0) {
    lines.push('待确认候选：无')
    return lines.join('\n')
  }
  lines.push(`待确认候选（${queue.length} 条）：`)
  for (const c of queue) {
    lines.push(
      `- ${c.scope_type ?? '?'} "${c.name ?? '?'}" ← ${c.signal_type ?? '?'}: `
      + `${c.signal_value ?? '?'}（置信度 ${fmtConfidence(c.confidence)}，出现 ${c.seen_count ?? 1} 次）`,
    )
  }
  return lines.join('\n')
}

/**
 * Render the result of a `memory_scope` action for the model.
 *
 * @param action - The action the call ran with.
 * @param value - The structured value that action returned.
 * @returns The model-visible text.
 */
export function renderScopeResult(action: string, value: unknown): string {
  switch (action) {
    case 'list':
      return renderScopeList(value as { scopes?: ScopeRow[] })
    case 'resolve':
      return renderScopeResolve(value as { resolution?: ScopeResolution; candidates?: ScopeCandidate[] })
    case 'create':
      return scopeLine(value as ScopeRow, '已创建（或已存在）作用域')
    case 'confirm': {
      const v = value as { scope_id?: number; confirmed?: boolean }
      return v.confirmed === false
        ? `作用域 [${v.scope_id ?? '?'}] 未确认（存储侧返回 confirmed=false）`
        : `已确认作用域 [${v.scope_id ?? '?'}]：此后该上下文直接解析到它，不再进候选队列。`
    }
    case 'alias_add': {
      const v = value as { scope_id?: number; alias?: string; alias_type?: string; added?: boolean }
      return v.added === false
        ? `别名 "${v.alias ?? '?'}" 已属于另一个作用域，未添加（一个别名只能指向一个作用域）。`
        : `已为作用域 [${v.scope_id ?? '?'}] 添加别名 "${v.alias ?? '?'}"（${v.alias_type ?? 'name'}）。`
    }
    case 'merge': {
      const v = value as {
        from?: number; to?: number; facts_moved?: number; aliases_moved?: number
        signals_moved?: number; children_moved?: number
      }
      return `已把作用域 [${v.from ?? '?'}] 合并进 [${v.to ?? '?'}]：迁移事实绑定 ${v.facts_moved ?? 0} 条、`
        + `别名 ${v.aliases_moved ?? 0} 个、信号 ${v.signals_moved ?? 0} 个、子作用域 ${v.children_moved ?? 0} 个。`
        + '源作用域保留为 merged 状态，历史仍可读。'
    }
    default:
      // Unreachable through `execute` (an unknown action throws before any RPC),
      // but `render` is also replayed over logged arguments: showing the raw
      // value beats an empty line when a stored action is not recognised.
      return JSON.stringify(value)
  }
}

/** Register all memory tools and return their disposers. */
export function registerMemoryTools(deps: ToolDeps): (() => void)[] {
  const { ctx, bridge } = deps
  const disposers: (() => void)[] = []
  const scope = deps.fallbackScope
  const call = <T>(method: string, params: Record<string, unknown>) =>
    bridge.call<T>(method, params)
  const budget = () => deps.resolveSummaryBudget?.() ?? deps.summaryTokens

  disposers.push(ctx.tools.register(defineTool({
    name: 'memory_add',
    description: '显式记住一条用户偏好、事实、事件、流程图或经验教训。传入原始内容，系统会自行抽取为原子事实；若与已存记忆冲突，系统会按证据强度决定替换或拒绝，并在结果里说明。',
    parameters: {
      content: { type: 'string', required: true, description: '要记住的原始内容' },
      user: { type: 'string', description: '可选：归属用户 id（默认当前用户，跨会话共享）' },
    },
    output: {
      schema: { type: 'object', additionalProperties: true },
      render(_args, value) {
        return [{ type: 'text', text: renderWriteReceipt(value as unknown as WriteReceipt) }]
      },
    },
    async execute(args, exec) {
      if (deps.isEnabled?.() === false) throw disabledError()
      const uid = args.user ?? userIdOf(exec, scope)
      const sid = sessionIdOf(exec, scope)
      const raw = args.content
      // Built once per call and spread into whichever path runs: the payload is
      // the same for all three, and reading the filesystem facts per branch
      // would be three chances to read three different answers.
      const scoped = scopeParam(deps, exec)
      // 1. LLM-first, exactly like the capture path: extract typed candidates
      //    in the dsh process (where the model lives) and persist them. This
      //    matters because the Python-side ``add`` path only runs the rule
      //    engine, which silently drops free-form facts (status 'skipped') that
      //    its narrow patterns do not match.
      if (deps.extract !== undefined) {
        try {
          const candidates = await deps.extract(raw)
          if (candidates.length > 0) {
            const r = await call<any>('persist_candidates', writeParams(deps, {
              user_id: uid,
              session_id: sid,
              turn_id: 0,
              candidates: stampExplicitPriority(candidates),
              ...scoped,
            }))
            return { ...r, candidate_id: r.candidate_id ?? '' }
          }
        } catch {
          /* fall through */
        }
      }
      // 2. Raw knowledge fallback. The caller explicitly asked to remember this
      //    content and extraction produced nothing — typically because a long
      //    body exceeded the extraction output budget and the truncated payload
      //    was discarded. Storing the text verbatim beats silently losing it.
      //    Gated on size so short utterances still take the rule path.
      const body = raw.trim()
      if (body.length >= RAW_KNOWLEDGE_MIN_CHARS) {
        const r = await call<any>('persist_candidates', writeParams(deps, {
          user_id: uid,
          session_id: sid,
          turn_id: 0,
          candidates: [rawKnowledgeCandidate(body)],
          ...scoped,
        }))
        return { ...r, candidate_id: r.candidate_id ?? '', fallback: 'raw' }
      }
      // 3. Rule path for short utterances.
      return await call<any>('add', writeParams(deps, {
        user_id: uid, session_id: sid, text: raw, turn_id: 0,
        ...scoped,
      }))
    },
  })))

  disposers.push(ctx.tools.register(defineTool({
    name: 'memory_replace',
    description: '用新内容替换一条已有记忆（按 fact_id 指名替换）。用于纠正写错的记忆：新内容会被抽取为事实，被指名的那条随之退役（superseded）。',
    parameters: {
      factId: { type: 'string', required: true, description: '要被替换的记忆 fact id（可用 memory_summary_detail 获取）' },
      content: { type: 'string', required: true, description: '替换后的新内容' },
      user: { type: 'string', description: '可选：归属用户 id（默认当前用户，跨会话共享）' },
    },
    output: {
      schema: { type: 'object', additionalProperties: true },
      render(_args, value) {
        return [{ type: 'text', text: renderWriteReceipt(value as unknown as WriteReceipt) }]
      },
    },
    async execute(args, exec) {
      if (deps.isEnabled?.() === false) throw disabledError()
      if (!args.factId) throw new Error('memory_replace requires factId')
      if (!args.content) throw new Error('memory_replace requires content')
      return await call<any>('replace', writeParams(deps, {
        user_id: args.user ?? userIdOf(exec, scope),
        fact_id: args.factId,
        new_text: args.content,
        ...scopeParam(deps, exec),
      }))
    },
  })))

  disposers.push(ctx.tools.register(defineTool({
    name: 'memory_recall',
    description: '检索与查询相关的持久记忆原子事实。查询用名词短语/关键词效果最好；若返回空，说明记忆库里没有足够相关的条目（而不是系统故障）。',
    parameters: {
      query: { type: 'string', required: true, description: '要检索的记忆查询' },
      user: { type: 'string', description: '可选：归属用户 id（默认当前用户，跨会话共享）' },
      topK: { type: 'integer', description: '返回条数上限（默认按配置）' },
    },
    output: {
      schema: { type: 'object', additionalProperties: true },
      render(_args, value) {
        const v = value as {
          facts?: Array<{
            fact_id?: string; subject?: string; predicate?: string
            object?: string; type?: string; content?: string | null
            truncated?: boolean
          }>
          degraded?: string[]
        }
        const facts = v.facts ?? []
        const blocks: string[] = []
        if (facts.length === 0) {
          blocks.push('（无相关记忆）')
        } else {
          // Render the full detail, not just the SPO title: for knowledge facts
          // (lesson / sop / few-shot) the body in `content` IS the answer, and
          // a render that omitted it would hide exactly what recall is for.
          blocks.push(facts.map((f) => {
            const head = [
              f.fact_id ? `[${f.fact_id}]` : '',
              `${f.subject ?? ''}${f.predicate ?? ''}: ${f.object ?? ''}`,
              f.type ? `*(${f.type})*` : '',
            ].filter(Boolean).join(' ')
            const body = (f.content ?? '').trim()
            if (!body) return `- ${head}`
            // A shortened body is flagged *with* the way to read the rest: a
            // truncation the model cannot follow up on is just missing data.
            const tail = f.truncated
              ? `\n    > （正文已截断，需要全文请用 memory_get factId=${f.fact_id ?? '?'}）`
              : ''
            return `- ${head}\n    > ${body}${tail}`
          }).join('\n'))
        }
        // A degraded index means the answer is partial for a *reason*; saying so
        // is the difference between "no memory matches" and "the search is
        // broken", which the model otherwise cannot tell apart.
        if ((v.degraded ?? []).length > 0) {
          blocks.push(`（注意：检索索引 ${(v.degraded ?? []).join(' / ')} 本次不可用，结果可能不完整）`)
        }
        return [{ type: 'text', text: blocks.join('\n') }]
      },
    },
    async execute(args, exec) {
      if (deps.isEnabled?.() === false) throw disabledError()
      const uid = args.user ?? userIdOf(exec, scope)
      const r = await call<any>('recall', {
        user_id: uid,
        query: args.query,
        token_budget: 4000,
        top_k: args.topK ?? deps.maxRecalledFacts,
        ...scopeParam(deps, exec),
      })
      return {
        facts: r.facts ?? [],
        token_count: r.token_count ?? 0,
        degraded: r.degraded ?? [],
      }
    },
  })))

  disposers.push(ctx.tools.register(defineTool({
    name: 'memory_get',
    description:
      '按 fact_id 读取一条记忆的完整内容（含未截断的知识正文）。'
      + 'memory_recall 为控制上下文会对单条过长的正文截断并标注，需要全文时用本工具。',
    parameters: {
      factId: { type: 'string', required: true, description: '记忆 fact id（memory_recall / memory_summary_detail 里可获得）' },
      user: { type: 'string', description: '可选：归属用户 id（默认当前用户，跨会话共享）' },
    },
    output: {
      schema: { type: 'object', additionalProperties: true },
      render(_args, value) {
        const v = value as {
          fact_id?: string; subject?: string; predicate?: string; object?: string
          type?: string; content?: string | null; status?: string
        }
        const head = `${v.subject ?? ''}${v.predicate ?? ''}: ${v.object ?? ''}`
        const meta = [v.fact_id ? `[${v.fact_id}]` : '', v.type ? `*(${v.type})*` : '',
          v.status ? `状态=${v.status}` : ''].filter(Boolean).join(' ')
        const body = (v.content ?? '').trim()
        return [{ type: 'text', text: `${head} ${meta}${body ? `\n\n${body}` : ''}` }]
      },
    },
    async execute(args, exec) {
      if (deps.isEnabled?.() === false) throw disabledError()
      if (!args.factId) throw new Error('memory_get requires factId')
      return await call('get_fact', {
        user_id: args.user ?? userIdOf(exec, scope),
        fact_id: args.factId,
      })
    },
  })))

  disposers.push(ctx.tools.register(defineTool({
    name: 'memory_summary',
    description:
      '渲染当前用户记忆的紧凑摘要（与注入系统提示词的快照同一预算、同一份渲染，但不含数据围栏）。'
      + '适合先看摘要，再按需用 memory_recall 查明细；要确认提示词里真正冻结的那段，用 memory_snapshot；要定位/编辑具体某条事实，用 memory_summary_detail。',
    parameters: {
      user: { type: 'string', description: '可选：归属用户 id（默认当前用户，跨会话共享）' },
    },
    output: {
      schema: { type: 'object', additionalProperties: true },
      render(_args, value) {
        const v = value as { text?: string }
        return [{ type: 'text', text: v.text ?? '' }]
      },
    },
    async execute(args, exec) {
      if (deps.isEnabled?.() === false) throw disabledError()
      const uid = args.user ?? userIdOf(exec, scope)
      const text = await call<string>('summary', {
        user_id: uid,
        // The injection budget, so what the model reads here is the text the
        // session prompt would freeze at this moment.
        max_tokens: budget(),
        detail: false,
        ...scopeParam(deps, exec),
      })
      return { text }
    },
  })))

  disposers.push(ctx.tools.register(defineTool({
    name: 'memory_snapshot',
    description:
      '返回当前会话系统提示词中真正冻结的那段记忆快照（含数据围栏原样）。'
      + '用于核对模型实际看到了什么；若本会话尚未冻结，会即时冻结并返回同一份文本。',
    parameters: {},
    output: {
      schema: { type: 'object', additionalProperties: true },
      render(_args, value) {
        const v = value as { text?: string; frozen?: boolean }
        const note = v.frozen === false ? '（尚未冻结：以下为即时渲染，开新会话将按此冻结）\n' : ''
        return [{ type: 'text', text: `${note}${v.text ?? '（无）'}` }]
      },
    },
    async execute(_args, exec) {
      if (deps.isEnabled?.() === false) throw disabledError()
      if (deps.snapshot === undefined) {
        return { text: '（本部署未启用快照注入）', frozen: false }
      }
      const sid = sessionIdOf(exec, scope)
      const existing = deps.snapshot.peek(sid)
      const text = existing ?? (await deps.snapshot.ensure(sid))
      if (!text) {
        return { text: '（无内容：记忆库为空，或注入已关闭）', frozen: existing !== undefined }
      }
      return { text, frozen: existing !== undefined }
    },
  })))

  disposers.push(ctx.tools.register(defineTool({
    name: 'memory_summary_detail',
    description:
      '渲染当前用户记忆的完整清单（每条含 fact_id，便于定位与编辑）。'
      + '注入系统提示词的是紧凑版（按类型分组、不含 fact_id）——如需确认注入内容，用 memory_snapshot。',
    parameters: {
      user: { type: 'string', description: '可选：归属用户 id（默认当前用户，跨会话共享）' },
    },
    output: {
      schema: { type: 'object', additionalProperties: true },
      render(_args, value) {
        const v = value as { text?: string }
        return [{ type: 'text', text: v.text ?? '' }]
      },
    },
    async execute(args, exec) {
      if (deps.isEnabled?.() === false) throw disabledError()
      const uid = args.user ?? userIdOf(exec, scope)
      const text = await call<string>('summary', {
        user_id: uid,
        max_tokens: deps.summaryTokens,
        detail: true,
        // The detail depth ignores the context in the Python renderer; passing
        // it anyway keeps one rule ("every read travels with its context")
        // instead of an exception a later reader has to re-derive.
        ...scopeParam(deps, exec),
      })
      return { text }
    },
  })))

  disposers.push(ctx.tools.register(defineTool({
    name: 'memory_forget',
    description: '删除一条记忆。默认软删除（retract，可被后续 backp 恢复）；purge=true 时彻底删除（含索引与复用证据，不可恢复）。',
    parameters: {
      factId: { type: 'string', description: '记忆 fact id（二选一）' },
      purge: { type: 'boolean', description: '是否彻底删除（默认 false：软删除）' },
      user: { type: 'string', description: '可选：归属用户 id（默认当前用户，跨会话共享）' },
    },
    output: {
      schema: { type: 'object', additionalProperties: true },
      render(_args, value) {
        return [{ type: 'text', text: renderWriteReceipt(value as unknown as WriteReceipt) }]
      },
    },
    async execute(args, exec) {
      if (deps.isEnabled?.() === false) throw disabledError()
      if (!args.factId) throw new Error('memory_forget requires factId')
      return await call<any>('forget', writeParams(deps, {
        user_id: args.user ?? userIdOf(exec, scope),
        fact_id: args.factId,
        purge: args.purge === true,
      }))
    },
  })))

  disposers.push(ctx.tools.register(defineTool({
    name: 'memory_user_md',
    description: '渲染当前用户的画像卡片 markdown（画像由用户手动维护的独立表渲染，不从活跃事实自动生成）。',
    parameters: {
      user: { type: 'string', description: '可选：归属用户 id（默认当前用户，跨会话共享）' },
    },
    output: {
      schema: { type: 'object', additionalProperties: true },
      render(_args, value) {
        const v = value as { text?: string }
        return [{ type: 'text', text: v.text ?? '' }]
      },
    },
    async execute(args, exec) {
      if (deps.isEnabled?.() === false) throw disabledError()
      const uid = args.user ?? userIdOf(exec, scope)
      const text = await call<string>('user_md', { user_id: uid })
      return { text }
    },
  })))

  disposers.push(ctx.tools.register(defineTool({
    name: 'memory_stats',
    description: '返回当前用户的记忆统计计数，以及最近几次写入的结果（含被拒绝的原因）。',
    parameters: {
      user: { type: 'string', description: '可选：归属用户 id（默认当前用户，跨会话共享）' },
    },
    output: {
      schema: { type: 'object', additionalProperties: true },
      render(_args, value) {
        const v = value as {
          facts?: number; pending?: number; archived?: number
          recent?: Array<{ status?: string; reject_kind?: string; reject_reason?: string }>
        }
        const lines = [
          `活跃记忆 ${v.facts ?? 0} 条 · 待处理 ${v.pending ?? 0} 条 · 归档 ${v.archived ?? 0} 条`,
        ]
        for (const entry of v.recent ?? []) {
          if (entry.reject_kind) {
            lines.push(`- 最近一次写入被拒绝（${entry.reject_kind}）：${entry.reject_reason ?? ''}`)
          }
        }
        return [{ type: 'text', text: lines.join('\n') }]
      },
    },
    async execute(args, exec) {
      if (deps.isEnabled?.() === false) throw disabledError()
      return await call('stats', { user_id: args.user ?? userIdOf(exec, scope) })
    },
  })))

  disposers.push(ctx.tools.register(defineTool({
    name: 'memory_scope',
    description:
      '查看与维护记忆的作用域层级（org / team / client / project / series / phase / document / thread）——作用域决定一条记忆归属哪里、以及在什么上下文里被召回，但不改变任何记忆的内容。'
      + 'action=list 列出作用域树；resolve 说明当前上下文解析到哪个作用域、还有哪些候选证据不足待确认；'
      + 'create 显式创建一个作用域（可带身份信号）；confirm 确认一个作用域（此后该上下文无需更多证据即解析到它）；'
      + 'alias_add 给作用域加一个别名；merge 把重复的两个作用域合并。',
    parameters: {
      action: {
        type: 'string',
        required: true,
        description: '要执行的操作：list / resolve / create / confirm / alias_add / merge',
      },
      scopeType: {
        type: 'string',
        description: 'create 必填：作用域类型（org / team / client / project / series / phase / document / thread）',
      },
      name: { type: 'string', description: 'create 必填：作用域的规范名（同名作用域已存在时返回已有的那个）' },
      parentId: { type: 'integer', description: 'create 可选：父作用域 id（省略则挂在根下）；list 可选：只看该父作用域的直接子作用域' },
      signals: {
        type: 'object',
        additionalProperties: true,
        description: 'create 可选：注册到该作用域上的身份信号，如 {"git_remote": "git@github.com:o/r.git"} 或 {"path": "D:/work/repo"}',
      },
      scopeId: { type: 'integer', description: 'confirm / alias_add 必填：目标作用域 id（来自 list / resolve / create）' },
      alias: { type: 'string', description: 'alias_add 必填：别名（另一个名字、路径或 remote）' },
      fromId: { type: 'integer', description: 'merge 必填：被合并掉的作用域 id' },
      toId: { type: 'integer', description: 'merge 必填：保留的作用域 id' },
      status: { type: 'string', description: 'list 可选：作用域状态（默认 active，也可用 merged / archived）' },
      user: { type: 'string', description: '可选：归属用户 id（默认当前用户，跨会话共享）' },
    },
    output: {
      schema: { type: 'object', additionalProperties: true },
      render(args, value) {
        return [{ type: 'text', text: renderScopeResult(args.action, value) }]
      },
    },
    async execute(args, exec) {
      if (deps.isEnabled?.() === false) throw disabledError()
      const uid = args.user ?? userIdOf(exec, scope)
      switch (args.action) {
        case 'list': {
          const scopes = await call<any>('scope_list', {
            ...(args.parentId !== undefined ? { parent_id: args.parentId } : {}),
            ...(args.status !== undefined && args.status !== '' ? { status: args.status } : {}),
          })
          return { scopes: scopes ?? [] }
        }
        case 'resolve': {
          const resolution = await call<any>('scope_resolve', {
            user_id: uid,
            session_id: sessionIdOf(exec, scope),
            // Read-only: asking what the context resolves to must not create a
            // scope as a side effect. Creation belongs to the write path (and to
            // an explicit `create`).
            create: false,
            ...scopeParam(deps, exec),
          })
          // The queue is accumulated across sessions, so it is a separate read
          // rather than a field of this resolution.
          const candidates = await call<any>('scope_unresolved', { user_id: uid })
          return { resolution: resolution ?? {}, candidates: candidates ?? [] }
        }
        case 'create': {
          if (!args.scopeType) throw new Error('memory_scope create requires scopeType')
          if (!args.name) throw new Error('memory_scope create requires name')
          return await call<any>('scope_create', {
            scope_type: args.scopeType,
            name: args.name,
            parent_id: args.parentId,
            signals: args.signals,
          })
        }
        case 'confirm': {
          if (args.scopeId === undefined) throw new Error('memory_scope confirm requires scopeId')
          return await call<any>('scope_confirm', { scope_id: args.scopeId })
        }
        case 'alias_add': {
          if (args.scopeId === undefined) throw new Error('memory_scope alias_add requires scopeId')
          if (!args.alias) throw new Error('memory_scope alias_add requires alias')
          return await call<any>('scope_alias_add', { scope_id: args.scopeId, alias: args.alias })
        }
        case 'merge': {
          if (args.fromId === undefined) throw new Error('memory_scope merge requires fromId')
          if (args.toId === undefined) throw new Error('memory_scope merge requires toId')
          return await call<any>('scope_merge', { from_id: args.fromId, to_id: args.toId })
        }
        default:
          throw new Error(
            `memory_scope: unknown action ${String(args.action)}`
            + '（可用：list / resolve / create / confirm / alias_add / merge）',
          )
      }
    },
  })))

  return disposers
}

/**
 * Resolve the **user** scope for a tool call.
 *
 * User scope must be stable across sessions so long-term memory is shared
 * (the write side captures under the fixed fallback scope, e.g. `global`); the
 * caller may still override with an explicit `user` argument. The session-aware
 * variant would isolate every session from every other one and memory would
 * never surface in a later session.
 */
function userIdOf(_exec: ToolRunContext, fallback: string): string {
  return fallback
}

/**
 * Resolve the **session** scope for a tool call (falls back to a scope).
 *
 * Used only for provenance (which session wrote the memory) and for locating a
 * session's frozen snapshot — never as the isolation scope: user isolation is
 * governed by {@link userIdOf}.
 */
function sessionIdOf(exec: ToolRunContext, fallback: string): string {
  const sessionId = exec.agent?.session?.id
  return sessionId !== undefined ? sessionId : fallback
}
