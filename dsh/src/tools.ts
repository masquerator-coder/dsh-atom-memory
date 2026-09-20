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

/** One label the store attached to a written fact. */
interface DomainLabel {
  name?: string
  confidence?: number
  is_primary?: boolean
  source?: string
}

/** Where a write was filed, and what it was labelled with. */
interface DomainAssignment {
  domains?: DomainLabel[]
  primary?: string | null
  unregistered?: string[]
  detail?: string
}

/** How the store may answer a write. Mirrors the Python write outcome. */
interface WriteReceipt {
  candidate_id?: string
  status?: string
  reject_kind?: string
  reject_reason?: string
  outcome?: {
    written?: string[]
    /** The scope the batch was filed under (see `ScopeResolution`). */
    scope?: { scope_id?: number; path?: string | null; display_name?: string | null; status?: string }
    /** One entry per surviving candidate, positionally aligned with the batch. */
    domains?: DomainAssignment[]
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
  const placement = renderPlacement(outcome.scope, outcome.domains)
  return placement ? `${parts.join('，')}。${placement}` : `${parts.join('，')}。`
}

/**
 * Render where a write was filed: its scope, and its topic labels.
 *
 * This exists because both dimensions are *guessed* by the store, and a guess
 * that is never shown cannot be corrected. Two things matter in the wording:
 * the primary label is named first (it is the one ranking and conflict judgement
 * read), and a topic the vocabulary did not hold is reported as *unregistered* —
 * the fact was stored under its nearest known ancestor, and only the user can
 * decide whether the new name is worth registering.
 *
 * @param scope - The scope the batch was filed under, when the store sent one.
 * @param assignments - One topic assignment per surviving candidate.
 * @returns A line to append to the receipt, or `''` when there is nothing to say.
 */
function renderPlacement(
  scope: NonNullable<WriteReceipt['outcome']>['scope'],
  assignments: DomainAssignment[] | undefined,
): string {
  const lines: string[] = []
  const path = scope?.path ?? scope?.display_name
  if (path) lines.push(`作用域：${path}${scope?.status === 'unresolved' ? '（未确认）' : ''}`)
  const labels = (assignments ?? []).flatMap(a => a.domains ?? [])
  if (labels.length > 0) {
    // One write can carry several candidates; show the distinct labels rather
    // than repeating the same topic once per fact.
    const seen = new Set<string>()
    const parts: string[] = []
    for (const label of labels) {
      const name = label.name ?? '?'
      if (seen.has(name)) continue
      seen.add(name)
      parts.push(label.is_primary ? `${name}（主）` : name)
    }
    if (parts.length > 0) lines.push(`主题：${parts.join(' · ')}`)
  }
  const unregistered = [...new Set((assignments ?? []).flatMap(a => a.unregistered ?? []))]
  if (unregistered.length > 0) {
    lines.push(
      `未注册的主题：${unregistered.join(' · ')}（已归入最近的已注册主题，`
      + '如需新建可用 memory_domains 的 create）',
    )
  }
  return lines.length > 0 ? `\n${lines.join('\n')}` : ''
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
  /**
   * Trigger one out-of-band overview refresh and report what happened.
   *
   * Optional: absent means the deployment does not maintain the overview, and
   * `memory_overview action=refresh` says so rather than silently doing nothing.
   * The returned token is one of the refresher's short outcome strings.
   */
  refreshOverview?: () => Promise<string>
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
    case 'promote': {
      const v = value as {
        from_scope_id?: number | null; to_scope_id?: number; action?: string
      }
      if (v.action === 'already-primary') {
        return `事实已主要归属作用域 [${v.to_scope_id ?? '?'}]，无需提升。`
      }
      const from = v.from_scope_id === undefined || v.from_scope_id === null
        ? '（原本没有主作用域）'
        : `[${v.from_scope_id}]`
      return `已把事实的主要归属从 ${from} 提升到 [${v.to_scope_id ?? '?'}]：`
        + '此后它在本作用域及其所有后代上下文中都能被召回，原归属降为次要绑定（未被删除）。'
    }
    default:
      // Unreachable through `execute` (an unknown action throws before any RPC),
      // but `render` is also replayed over logged arguments: showing the raw
      // value beats an empty line when a stored action is not recognised.
      return JSON.stringify(value)
  }
}

/**
 * Render the result of a `memory_domains` action for the model.
 *
 * @param action - The action the call ran with.
 * @param value - The structured value that action returned.
 * @returns The model-visible text.
 */
export function renderDomainResult(action: string, value: unknown): string {
  switch (action) {
    case 'list': {
      const rows = (value as { domains?: Array<Record<string, unknown>> }).domains ?? []
      if (rows.length === 0) return '（主题词表为空；写一条记忆或维护一个项目后会自动生成）'
      const lines = rows.map((row) => {
        const path = String(row.path ?? row.name ?? '?')
        const depth = Math.max(0, path.split('/').filter(p => p.length > 0).length - 1)
        const display = row.display_name && row.display_name !== row.name ? `（${row.display_name}）` : ''
        const seeded = row.system_seeded === true ? ' · 自动生成' : ''
        return `${'  '.repeat(depth)}- [${row.domain_id ?? '?'}] ${path}${display}${seeded}`
      })
      return [`主题词表（${rows.length} 个，按层级缩进）：`, ...lines].join('\n')
    }
    case 'resolve': {
      const v = value as {
        session?: { names?: string[]; source?: string; detail?: string }
        proposals?: Array<{ proposed?: string; resolved?: string | null; ancestors?: string[] }>
        unresolved?: string[]
      }
      const lines: string[] = []
      const session = v.session ?? {}
      const names = session.names ?? []
      lines.push(
        names.length > 0
          ? `当前会话主题：${names.join(' · ')}（依据 ${session.source ?? '?'}${session.detail ? `：${session.detail}` : ''}）`
          : '当前会话没有解析到任何主题（写入时会退回 general）。',
      )
      for (const proposal of v.proposals ?? []) {
        lines.push(
          proposal.resolved
            ? `- "${proposal.proposed ?? '?'}" → ${proposal.resolved}`
              + (proposal.ancestors && proposal.ancestors.length > 0
                ? `（上级：${proposal.ancestors.join(' / ')}）`
                : '')
            : `- "${proposal.proposed ?? '?'}"：未注册，会归入最近的已注册祖先（若无则退到 general）`,
        )
      }
      if (v.unresolved && v.unresolved.length > 0) {
        lines.push(`待注册：${v.unresolved.join(' · ')}`)
      }
      return lines.join('\n')
    }
    case 'create': {
      const v = value as { domain_id?: number; path?: string; name?: string; display_name?: string }
      return `已注册主题 [${v.domain_id ?? '?'}] ${v.path ?? v.name ?? '?'}`
        + `${v.display_name && v.display_name !== v.name ? `（${v.display_name}）` : ''}。`
    }
    case 'rename': {
      const v = value as { domain_id?: number; from?: string; to?: string; children_moved?: number }
      return `已把主题 [${v.domain_id ?? '?'}] 从 "${v.from ?? '?'}" 改名为 "${v.to ?? '?'}"`
        + `（已有标记按 id 关联，未改动；下级主题 ${v.children_moved ?? 0} 个路径已同步）。`
    }
    case 'merge': {
      const v = value as {
        from?: number; to?: number; labels_moved?: number; children_moved?: number
      }
      return `已把主题 [${v.from ?? '?'}] 合并进 [${v.to ?? '?'}]：关联标记 ${v.labels_moved ?? 0} 条、`
        + `下级 ${v.children_moved ?? 0} 个。源主题保留为 merged 状态，历史仍可读。`
    }
    case 'signal_reject': {
      const v = value as { name?: string; rejected?: number }
      return v.rejected
        ? `已忽略待注册建议 "${v.name ?? '?'}"。`
        : `没有找到待注册建议 "${v.name ?? '?'}"。`
    }
    case 'bridge_add': {
      const v = value as { from?: number; to?: number; added?: boolean }
      return v.added
        ? `已记录主题桥接 [${v.from ?? '?'}] → [${v.to ?? '?'}]：只在排序上加权，不改变过滤集合。`
        : `桥接未记录（起点或终点主题不存在，或两者相同）。`
    }
    case 'archive': {
      const v = value as { domain_id?: number; archived?: boolean }
      return v.archived === false
        ? `主题 [${v.domain_id ?? '?'}] 未归档（可能不存在或已归档）。`
        : `已归档主题 [${v.domain_id ?? '?'}]：它不再被建议给新的记忆，已有标记不受影响。`
    }
    case 'unresolved': {
      const signals = (value as { signals?: Array<Record<string, unknown>> }).signals ?? []
      if (signals.length === 0) return '（没有待注册的主题建议）'
      const lines = signals.map(
        s => `- ${s.name ?? '?'}（出现 ${s.seen_count ?? 1} 次`
          + `${s.nearest_ancestor ? `，当前归入 [${s.nearest_ancestor}]` : ''}）`,
      )
      return ['待注册的主题建议（达到一定次数后由用户决定是否注册）：', ...lines].join('\n')
    }
    case 'fact_set':
    case 'fact_get': {
      const v = value as {
        fact_id?: string
        primary?: string | null
        domains?: Array<{ name?: string; is_primary?: boolean; source?: string }>
      }
      if (!v.domains || v.domains.length === 0) {
        return `事实 ${v.fact_id ?? '?'} 没有主题标记（早于主题维度写入的记忆）。`
      }
      const parts = v.domains.map(
        d => `${d.name ?? '?'}${d.is_primary ? '（主）' : ''}·${d.source ?? '?'}`,
      )
      return `事实 ${v.fact_id ?? '?'} 的主题：${parts.join(' · ')}`
    }
    default:
      return JSON.stringify(value)
  }
}

/** One row of the memory changelog, as `changes` reports it. */
export interface ChangeRow {
  type?: string
  created_at?: number
  detail?: Record<string, unknown>
}

/** Human labels for the changelog's event types. */
const CHANGE_LABELS: Record<string, string> = {
  fact_written: '写入',
  fact_deduplicated: '去重合并',
  fact_superseded: '替换',
  fact_retracted: '软删除',
  fact_purged: '彻底删除',
  fact_reinforced: '复用加强',
  scope_created: '新建范围',
  scope_updated: '范围更新',
  domain_created: '新建主题',
  domain_updated: '主题更新',
  domain_merged: '主题合并',
  fact_rejected: '丢弃',
}

/** Render a timestamp as a local `MM-DD HH:mm` stamp (falling back to the raw value). */
function formatChangeTime(ms: number | undefined): string {
  if (typeof ms !== 'number' || !Number.isFinite(ms) || ms <= 0) return '?'
  const d = new Date(ms)
  if (Number.isNaN(d.getTime())) return '?'
  const pad = (n: number): string => String(n).padStart(2, '0')
  return `${pad(d.getMonth() + 1)}-${pad(d.getDate())} ${pad(d.getHours())}:${pad(d.getMinutes())}`
}

/**
 * Render the changelog as a readable list.
 *
 * @param changes - Rows from Python, already newest-first.
 * @param level - The change level the same call computed, when present.
 * @returns A markdown-ish block; never empty (an empty store says so).
 */
export function renderChanges(changes: ChangeRow[], level?: string): string {
  if (changes.length === 0) return '（近期没有记忆变动）'
  const lines = changes.map((row) => {
    const label = CHANGE_LABELS[row.type ?? ''] ?? row.type ?? '?'
    const detail = row.detail ?? {}
    // Name the thing that changed, using whatever the payload actually carries:
    // a predicate reads best, a scope/domain name next, an id last.
    const subject =
      (typeof detail.predicate === 'string' && detail.predicate)
      || (typeof detail.object === 'string' && detail.object)
      || (typeof detail.name === 'string' && detail.name)
      || (typeof detail.fact_id === 'string' && detail.fact_id)
      || ''
    const extra = typeof detail.type === 'string' ? `（${detail.type}）` : ''
    return `- ${formatChangeTime(row.created_at)} ${label}${extra}${subject ? `：${subject}` : ''}`
  })
  const header = level === undefined ? '' : `变动级别：${level}\n`
  return `${header}${lines.join('\n')}`
}

/** Render the overview cache's status, including whether a refresh is warranted. */
export function renderOverviewStatus(status: Record<string, unknown>): string {
  const cached = status.cached === true
  const lines: string[] = []
  lines.push(cached ? '总览：已缓存' : '总览：尚无缓存（当前渲染的是确定性回退版本）')
  if (cached) {
    const source = typeof status.source === 'string' ? status.source : '?'
    const facts = typeof status.facts_count === 'number' ? status.facts_count : 0
    const updated = formatChangeTime(status.updated_at as number | undefined)
    lines.push(`来源：${source} · 覆盖 ${facts} 条 · 生成于 ${updated}`)
  }
  if (status.stale === true) lines.push('状态：已过期（记忆库在生成后发生了结构性变化）')
  const reason = typeof status.refresh_reason === 'string' ? status.refresh_reason : ''
  const reasonText: Record<string, string> = {
    no_facts: '记忆库为空，无需生成',
    not_cached: '尚无缓存，值得生成',
    level: '发生了结构性变化，值得重新生成',
    up_to_date: '仅细节变化，无需重新生成',
  }
  if (reason) lines.push(`判定：${reasonText[reason] ?? reason}`)
  return lines.join('\n')
}

/** Translate the refresher's outcome token into a sentence. */
export function renderRefreshOutcome(outcome: string): string {
  if (outcome === 'refreshed') return '已重新生成并写入缓存（下个会话生效）。'
  if (outcome === 'throttled') return '距上次刷新太近，已跳过；稍后再试。'
  if (outcome === 'no-model') return '未配置可用模型，无法生成。'
  if (outcome === 'nothing-to-narrate') return '记忆库暂无可叙述的内容。'
  if (outcome === 'empty-generation') return '模型没有产出内容，缓存保持不变。'
  if (outcome === 'skipped') return '总览后台生成未启用，或记忆功能已关闭。'
  if (outcome === 'error') return '生成失败（详见日志），缓存保持不变。'
  if (outcome.startsWith('no-change:')) {
    const reason = outcome.slice('no-change:'.length)
    return reason === 'up_to_date'
      ? '仅细节变化，无需重新生成。'
      : `无需重新生成（${reason}）。`
  }
  return outcome
}

/**
 * Extract just the overview head from a full compact render.
 *
 * `memory_summary` and `memory_overview` share one render; this keeps the second
 * from returning the first's digest, which would make the two tools
 * indistinguishable to the model.
 *
 * The head ends where the reference detail begins. That boundary used to be the
 * `## 要了解细节` lookup guide, which sat between them; the guide is gone (tool
 * usage lives in the tool schemas), so the head now runs to the detail digest's
 * first section label.
 *
 * @param text - A compact render with the overview head enabled.
 * @returns The overview section, up to the reference detail.
 */
export function overviewHeadOf(text: string): string {
  const start = text.indexOf('## 以前做过的工作')
  if (start < 0) {
    // No head: either an empty store or an older Python side. Say so rather
    // than returning the digest under a heading that would misdescribe it.
    return text.trim() === '' ? '（无）' : text
  }
  // The detail digest renders one `## <section>` label per memory type; the head
  // carries its own label (`## 以前做过的工作`) and no other. So the first
  // section label *after* the head's own line is where the detail starts.
  const rest = text.slice(start)
  const nextLabel = rest.indexOf('\n## ', '## 以前做过的工作'.length)
  return (nextLabel < 0 ? rest : rest.slice(0, nextLabel)).trim()
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
      + '摘要先给「以前做过的工作」总览，最后是按类型的明细；工具用法写在各 memory_* 工具自己的定义里，摘要不再重复。'
      + '适合先看总览，再按需用 memory_recall 查明细；要看完整清单用 memory_summary_detail；'
      + '要确认提示词里真正冻结的那段，用 memory_snapshot；要查记忆库近期变动，用 memory_overview action=changes。',
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
      const raw = await call<string | { text?: string }>('summary', {
        user_id: uid,
        // The injection budget, so what the model reads here is the text the
        // session prompt would freeze at this moment.
        max_tokens: budget(),
        detail: false,
        // Same head the injected snapshot uses. This tool's whole purpose is to
        // show what the model is being told, so rendering it without the head
        // would make the tool disagree with the prompt it claims to mirror.
        overview: true,
        ...scopeParam(deps, exec),
      })
      // Tolerate a plain-string reply from an older Python side.
      return { text: typeof raw === 'string' ? raw : (raw?.text ?? '') }
    },
  })))

  disposers.push(ctx.tools.register(defineTool({
    name: 'memory_overview',
    description:
      '查看与维护「以前做过的工作」总览，以及记忆库的变动记录。'
      + 'action=show（默认）返回当前总览正文；status 返回缓存状态与是否值得重新生成；'
      + 'changes 列出近期记忆变动（写入/去重/替换/退役等，按时间倒序）——'
      + '想知道"记忆库最近有什么变化"就用它。'
      + 'refresh 会立刻重新生成总览（会调用一次模型，仅在用户明确要求时使用）。',
    parameters: {
      action: {
        type: 'string',
        description: 'show | refresh | status | changes（默认 show）',
      },
      since: { type: 'string', description: 'action=changes 时可选：只看该时间戳（毫秒）之后的变动' },
      limit: { type: 'string', description: 'action=changes 时可选：最多返回几条（默认 50）' },
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
      const action = (args.action ?? 'show').trim().toLowerCase()

      if (action === 'refresh') {
        if (deps.refreshOverview === undefined) {
          return { text: '（本部署未启用总览后台生成，无法手动刷新）' }
        }
        const outcome = await deps.refreshOverview()
        return { text: `总览刷新结果：${renderRefreshOutcome(outcome)}` }
      }

      if (action === 'status') {
        const status = await call<Record<string, unknown>>('overview_status', {
          user_id: uid,
        })
        return { text: renderOverviewStatus(status) }
      }

      if (action === 'changes') {
        const since = Number.parseInt(args.since ?? '', 10)
        const limit = Number.parseInt(args.limit ?? '', 10)
        const result = await call<{ changes?: ChangeRow[]; level?: string }>('changes', {
          user_id: uid,
          ...(Number.isFinite(since) ? { since_ms: since } : {}),
          ...(Number.isFinite(limit) ? { limit } : {}),
        })
        return { text: renderChanges(result.changes ?? [], result.level) }
      }

      if (action !== 'show') {
        throw new Error(`memory_overview: unknown action "${args.action}"`)
      }

      const raw = await call<string | { text?: string }>('summary', {
        user_id: uid,
        max_tokens: budget(),
        detail: false,
        overview: true,
        ...scopeParam(deps, exec),
      })
      const text = typeof raw === 'string' ? raw : (raw?.text ?? '')
      // Show only the overview head: the digest below it is what `memory_summary`
      // is for, and repeating it here would make the two tools indistinguishable.
      return { text: overviewHeadOf(text) }
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
      + 'alias_add 给作用域加一个别名；merge 把重复的两个作用域合并；'
      + 'promote 把一条事实的主要归属提升到更通用的作用域（例如从某个文档提升到项目或用户级），'
      + '让"这条经验适用于所有同类工作"立即生效——事实被绑在文档上时，从同级的其他文档里是召回不到的。',
    parameters: {
      action: {
        type: 'string',
        required: true,
        description: '要执行的操作：list / resolve / create / confirm / alias_add / merge / promote',
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
      factId: {
        type: 'string',
        description: 'promote 必填：要提升的事实 id（来自 memory_recall / memory_list_facts 的结果）',
      },
      toScopeId: {
        type: 'integer',
        description: 'promote 必填：提升到哪个作用域 id（必须比该事实当前的主作用域更通用，来自 list / resolve）',
      },
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
        case 'promote': {
          if (!args.factId) throw new Error('memory_scope promote requires factId')
          if (args.toScopeId === undefined) throw new Error('memory_scope promote requires toScopeId')
          return await call<any>('fact_scope_promote', {
            user_id: uid,
            fact_id: args.factId,
            to_scope_id: args.toScopeId,
          })
        }
        default:
          throw new Error(
            `memory_scope: unknown action ${String(args.action)}`
            + '（可用：list / resolve / create / confirm / alias_add / merge / promote）',
          )
      }
    },
  })))

  disposers.push(ctx.tools.register(defineTool({
    name: 'memory_domains',
    description:
      '查看与维护记忆的主题词表（teaching / programming / life 等）——主题说明一条记忆"关于什么"，'
      + '与作用域（"在什么上下文"）正交：作用域靠环境自动解析，主题由抽取建议 + 词表校验得到。'
      + '一条事实只能属于一个主作用域，但可以有多个主题（主主题参与排序与冲突判定）。'
      + '词表是注册制：抽取建议了未注册的主题时，会归入最近的已注册祖先并记入待注册队列，不会自动新建。'
      + 'action=list 列出词表；resolve 说明当前会话解析到哪些主题、某个主题名会归到哪里；create 注册新主题；'
      + 'rename / merge / archive 维护词表；bridge_add 记录跨主题关联（只影响排序权重，不改变过滤）；'
      + 'unresolved 列出待注册队列；signal_reject 忽略某个待注册建议；'
      + 'fact_set / fact_get 读取或改写某条事实的主题（改写是权威的：未列出的主题会被移除）。',
    parameters: {
      action: {
        type: 'string',
        required: true,
        description:
          '要执行的操作：list / resolve / create / rename / merge / archive / bridge_add / '
          + 'unresolved / signal_reject / fact_set / fact_get',
      },
      name: { type: 'string', description: 'create / rename / signal_reject 必填：主题规范名（小写 ASCII，斜杠分隔，如 teaching/ds）' },
      displayName: { type: 'string', description: 'create 可选：给人看的中文显示名' },
      labels: {
        type: 'array',
        items: { type: 'string' },
        description: 'resolve 必填：要解析的主题名列表；fact_set 时是新的主题列表（第一个为主主题）',
      },
      domainId: { type: 'integer', description: 'rename / archive / bridge_add 必填：目标主题 id（来自 list）' },
      fromId: { type: 'integer', description: 'merge / bridge_add 必填：被合并掉 / 起点主题 id' },
      toId: { type: 'integer', description: 'merge / bridge_add 必填：保留 / 终点主题 id' },
      weight: { type: 'number', description: 'bridge_add 可选：桥接权重（0..1，默认 0.5）' },
      parentId: { type: 'integer', description: 'create 可选：父主题 id（省略则自动挂到最近的已注册祖先，或 general）' },
      factId: { type: 'string', description: 'fact_set / fact_get 必填：事实 id' },
      status: { type: 'string', description: 'list 可选：主题状态（默认 active，也可用 merged / archived）' },
      user: { type: 'string', description: '可选：归属用户 id（默认当前用户，跨会话共享）' },
    },
    output: {
      schema: { type: 'object', additionalProperties: true },
      render(args, value) {
        return [{ type: 'text', text: renderDomainResult(args.action, value) }]
      },
    },
    async execute(args, exec) {
      if (deps.isEnabled?.() === false) throw disabledError()
      const uid = args.user ?? userIdOf(exec, scope)
      switch (args.action) {
        case 'list': {
          const domains = await call<any>('domain_list', {
            user_id: uid,
            ...(args.status !== undefined && args.status !== '' ? { status: args.status } : {}),
          })
          return { domains: domains ?? [] }
        }
        case 'resolve': {
          const resolution = await call<any>('domain_resolve', {
            user_id: uid,
            labels: args.labels ?? [],
            ...scopeParam(deps, exec),
          })
          return resolution ?? {}
        }
        case 'create': {
          if (!args.name) throw new Error('memory_domains create requires name')
          return await call<any>('domain_create', {
            user_id: uid,
            name: args.name,
            display_name: args.displayName ?? '',
            parent_id: args.parentId,
          })
        }
        case 'rename': {
          if (args.domainId === undefined) throw new Error('memory_domains rename requires domainId')
          if (!args.name) throw new Error('memory_domains rename requires name')
          return await call<any>('domain_rename', {
            user_id: uid, domain_id: args.domainId, name: args.name,
          })
        }
        case 'merge': {
          if (args.fromId === undefined) throw new Error('memory_domains merge requires fromId')
          if (args.toId === undefined) throw new Error('memory_domains merge requires toId')
          return await call<any>('domain_merge', {
            user_id: uid, from_id: args.fromId, to_id: args.toId,
          })
        }
        case 'archive': {
          if (args.domainId === undefined) throw new Error('memory_domains archive requires domainId')
          return await call<any>('domain_archive', { user_id: uid, domain_id: args.domainId })
        }
        case 'bridge_add': {
          if (args.fromId === undefined) throw new Error('memory_domains bridge_add requires fromId')
          if (args.toId === undefined) throw new Error('memory_domains bridge_add requires toId')
          return await call<any>('domain_bridge_add', {
            user_id: uid,
            from_id: args.fromId,
            to_id: args.toId,
            weight: args.weight ?? 0.5,
          })
        }
        case 'unresolved':
          return { signals: (await call<any>('domain_unresolved', { user_id: uid })) ?? [] }
        case 'signal_reject': {
          if (!args.name) throw new Error('memory_domains signal_reject requires name')
          return await call<any>('domain_signal_reject', { user_id: uid, name: args.name })
        }
        case 'fact_set': {
          if (!args.factId) throw new Error('memory_domains fact_set requires factId')
          if (!args.labels || args.labels.length === 0) {
            throw new Error('memory_domains fact_set requires labels')
          }
          return await call<any>('fact_domain_set', {
            user_id: uid, fact_id: args.factId, domains: args.labels,
          })
        }
        case 'fact_get': {
          if (!args.factId) throw new Error('memory_domains fact_get requires factId')
          return await call<any>('fact_domain_get', { user_id: uid, fact_id: args.factId })
        }
        default:
          throw new Error(
            `memory_domains: unknown action ${String(args.action)}`
            + '（可用：list / resolve / create / rename / merge / archive / bridge_add / '
            + 'unresolved / signal_reject / fact_set / fact_get）',
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
