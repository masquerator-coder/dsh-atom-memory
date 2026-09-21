# 代码全面审查报告（第二轮）— dsh-atom-memory

审核对象：`D:\Coding\DSH-Plugin\dsh-atom-memory` @ `bbb6724`
审核范围：Python 后端 `atom_memory/`（18 模块 + 13 个迁移）+ TS 插件 `dsh/src/`（21 文件）+ `dsh/src/client/` UI
审核方式：4 个并行子代理分模块深读 + lead 逐条独立复现
重点关注：正确性与边界条件 · 并发与状态 · 性能 · 向后兼容与公共契约

---

## 一、基线实测（本机实跑）

| 检查项 | 命令 | 结果 |
| --- | --- | --- |
| Python 测试 | `python -m pytest -q` | **573 passed, 1 skipped**（39.21s） |
| 前端测试 | `pnpm --dir dsh test` | **307 passed / 19 files**（3.41s） |
| 类型检查 | `pnpm --dir dsh typecheck` | 干净，exit 0 |
| 迁移编号连续性 | 001–013 连续，无缺号 | 通过（`009` 为显式 no-op，有意保留） |
| Remote 契约对齐 | `remote.ts` 17 个描述符 ↔ `controller.ts` `@Remote` | 逐一对齐，无遗漏/多余 |

> 子代理曾报告 `test_rpc.py::test_persist_candidates_persists_type_and_content` 失败。**lead 复跑该文件 15 passed**，判定为**负载敏感的偶发失败**，非缺陷。

**审查结论：不通过 — 5 个阻塞项**（4 个 Python + 1 个 TS/UI），另有 11 个建议项、9 个风格项。

> 上述为**审查当时**的判定。其中 4 个阻塞项已修复、1 个（B-4）经复核为误报，详见紧随其后的「修复结果」。当前状态：**阻塞项已清零**，可通过。

---

## 修复结果（修复轮次回填）

修复批：B-1/B-2/B-3/B-5 + S-1/S-2/S-3/S-5（含测试）。B-4 经复核为**误报**。

| 项 | 状态 | 修复位置 | 验证证据 |
| --- | --- | --- | --- |
| B-1 | 已修复 | `worker.py:2306-2312`（按可归档列表推导缺口）、`:2334`（事件上报 `unreachable`）、`:2341`（日志上报） | 8 保护 + 2 可归档 / cap=3 → `archived=2, unreachable=5`；全保护时 `archived=0` 但**仍上报** `unreachable=3`（原先静默）；无保护路径 `archived=7, unreachable=0`（无回归） |
| B-2 | 已修复 | `summary.py:1686-1716`（`assemble()` 度量组装后成品） | 预算 60/80/120/200/400/800/1500 全部 ≤ 限额；修复前 20/50/100/200/500 分别超出 +38/+41/+46/+50/+60 |
| B-3 | 已修复 | `api.py:72-113`（`_merge_truncation_records`，按 `field/original_chars/kept_chars` 去重） | 重叠记录 2→1；不同字段（`object` + `content`）仍保留 2 条；不同裁剪长度视为不同事件 |
| B-4 | **误报，已回退** | 无（代码原本正确） | 见下方说明 |
| B-5 | 已修复 | `memory-settings-controller.ts:398-450`（只发脏字段、不发 `type`）+ `:329-361`（`saveFact`/`deleteFact` 补 `unwrap` + 重抛）+ `MemorySettingsSection.tsx:701-716,727`（失败保留编辑器并显示原因） | 未改动行不再产生任何写调用；改 `object` 只发 `{user,fact_id,object}`；失败时 modal 保持打开且错误可见 |
| S-1 | 已修复 | `domain.py:1450-1467`（`is_primary = excluded.is_primary` + 同事务内降级其他行） | 两次 primary 赋值后 `fact_domain` 仅 1 行 `is_primary=1`；在位者保持 standing |
| S-2 | 已修复 | `domain.py:1487-1497`（快照随写入刷新，上限对**本次调用**计数） | 单次 `attach()` 携带 4 个标签 / cap=2 → 实际落库 2 条 |
| S-3 | 已修复 | `scope.py:1207-1228`（WHERE 补齐 `signal_type` + `normalized_value`） | 同名不同信号的兄弟候选保持 `pending`，不再被误标 `promoted` |
| S-5 | 已修复 | `retriever.py:679-718`（先预留省略号成本再走循环） | 限额 0..119 × 8 种字符形态共 960 组合，**0 处越界**（修复前 21 处） |

### 关于 B-4 的更正

原报告将「`rebuild_fact_reinforcement` 持久化的是**末次事件时刻**的快照，而读取路径按 `adjust(n, last_used_at, now)` 衰减到当下」判为缺陷，并据此改动了写入值。**该判定错误**：

`adjust` 的 docstring 明确规定 `facts.reinforce_count` 是 *"the value as of `last_used_at`, not now"*。列里存的就是快照，读取时再向前衰减一次。原探针把 *rebuild 的存储值* 与 *增量路径的读取值* 两个不同量做了比较，才得出「5 天衰减被抹掉」的结论。按正确基准复测：rebuild 存储 `1.0`、`last_used_at` 保持事件时刻，读取得 `0.9548`，与增量路径完全一致。

代码本就正确，**已回退该改动**；仓库里 85 个 reinforce 测试（含 `test_rebuild_reproduces_the_incremental_aggregate`）编码的正是这个不变量，它们在我引入「修复」后立刻失败，这也正是回归测试应有的作用。

### 修复后的基线

| 检查项 | 修复前 | 修复后 |
| --- | --- | --- |
| `python -m pytest -q` | 573 passed, 1 skipped | **587 passed, 1 skipped**（+14 新测试） |
| `pnpm --dir dsh test` | 307 passed / 19 files | **313 passed / 19 files**（+6 新测试） |
| `pnpm --dir dsh typecheck` | 干净 | **干净，exit 0** |

新增测试均经过**反向验证**：临时还原缺陷实现后对应用例必须失败（B-1、S-3、B-5-modal 三项已逐一实测确认）。此前两处零覆盖区域已补齐 —— `truncate_to_tokens` 与 `saveAllFacts`。

未纳入本次修复的 S-4、S-6..S-11 仍按原报告保持「建议项」状态。

---

## 二、阻塞项（必须修复）

### B-1 容量上限静默失效 —— `max_active_facts` 在最需要它的场景是空操作

**位置**：`atom_memory/worker.py:2275`（`overflow` 计算）与 `:2296`（切片）

```python
overflow = len(owner_rows) - cap          # 2275: 按「全部 active」计
if overflow <= 0: continue
candidates = []
for row in owner_rows:
    if int(row["created_at"]) >= cutoff: continue          # 2280 太新，跳过
    if float(row["reinforce_count"] or 0.0) > 0.0: continue # 2282 有复用证据，跳过
    if (row["type"] or "semantic") in durable: continue     # 2284 耐久类型，跳过
    candidates.append(...)
candidates.sort()
for ... in candidates[:overflow]:         # 2296: 切片索引的是「过滤后」列表
```

`overflow` 按全部 active 事实计算，但切片作用于**过滤后的** `candidates`。当受保护事实数 > overflow 时，`max_active_facts` 无法达成，而函数**照常报告成功**。

**实测（lead 独立探针）**：10 条 active（8 条年轻受保护 + 2 条可归档），`max_active_facts=3`：

```
active before = 10, cap = 3
archived = 2; active after = 8 (cap 3)
Capacity pass archived 2 fact(s) over cap 3      ← 日志宣称成功
```

**影响**：容量策略完全失效，且**无声**——调用方/日志都认为已执行。这正是该策略唯一的用途场景。

**建议修复**：改由可归档列表推导缺口，并把「无法达标」显式上报：

```python
overflow = max(0, len(owner_rows) - cap)
archivable = candidates[:min(overflow, len(candidates))]
unreachable = overflow - len(archivable)   # 上报，不静默
```

---

### B-2 注入快照的 detail 深度系统性超出其文档承诺的硬上限

**位置**：`atom_memory/summary.py:1685`（标题）与 `:1700-1703`（页脚），预算循环 `:1690-1698`

```python
budget = max_tokens                       # 1686: 未扣标题成本
for fact in facts:
    cost = estimate_tokens(line)
    if cost > budget and kept > 0: break
    lines.append(line); budget -= cost
lines.append("")                          # 1700: 页脚无条件下追加
lines.append(f"> {kept} 条事实 (facts) · 含 fact_id 作为唯一引用")
if budget_exhausted:
    lines.append(f"> ⚠ 超出 token 预算，已裁剪（限制 {max_tokens}）")   # 越超越长
```

标题与页脚在预算循环**之后**无条件追加且从不计入。文档自相矛盾：`summary.py:1677` 称「预算约束的是 **facts**」，而 `api.py:1205` 承诺「Estimated token cap for the rendered text, **footer included**」。

**实测（lead 独立探针，400 条属性事实）**：

| `max_tokens` | 实际 tokens | 超出 |
| --- | --- | --- |
| 20 | 58 | +38 |
| 50 | 91 | +41 |
| 100 | 146 | +46 |
| 200 | 250 | +50 |
| 500 | 560 | +60 |

**影响**：该文本被冻结进会话系统提示词，**每次请求都付费**；且超出量随预算增长（页脚字数随 `max_tokens` 增长）。

**建议修复**：与同文件 `_select`（`:1578-1598`，它**确实**度量了含页脚的成品）保持一致——度量**组装后**的成品再决定取舍：

```python
def assemble(kept): ...   # 含标题 + 正文 + 页脚
for fact in facts:
    if estimate_tokens(assemble(kept + [line])) > max_tokens and kept: break
```

**lead 验证**：该改法在 50/100/200/500/1500 各档全部达标。
**固有下限**：`max_tokens < 36` 时无解（标题+页脚最小成本 36 tokens），该区间应显式拒绝或文档化，而非视为缺陷。

---

### B-3 截断记录在写回执中重复计数（`wait_ms > 0` 时）

**位置**：`atom_memory/api.py:334`

```python
if truncated:
    outcome["truncated"] = list(outcome.get("truncated") or []) + list(truncated)
```

worker 已把同一批记录写进 outcome（`worker.py:761` `outcome["truncated"].extend(result.truncated_fields)`），并经 `worker.py:1120` 持久化到 `fact_candidates.result_fact_ids`；`_write_receipt` 读回后再追加一次调用方列表。

**实测（lead 独立探针）**：存储侧已含 1 条 `content` 记录，调用方再传同一条：

```
merged truncated = [{"field":"content","original_chars":400,"kept_chars":30},
                    {"field":"content","original_chars":400,"kept_chars":30}]
count = 2      ← 同一次截断被报告两次
```

**关键条件**：`wait_ms > 0` 才会走到 `:333-334`；`wait_ms = 0` 时在 `:320-321` 提前返回，**不触发**（故默认配置下不可见——这也是测试未覆盖的原因）。

**注意（子代理结论的部分修正）**：在 `api.add` 的常规路径上两列表**通常按字段互斥**（`api.py:254` 只记 `content`，worker 阶段常记 `object`），故**并非每次都重复**。但重叠条件真实存在且已复现，且一旦重复即同一损失报两遍。

**建议修复**：按 `(field, original_chars, kept_chars)` 去重后再合并，或当行为终态时不再追加调用方列表。

---

### B-4 `rebuild_fact_reinforcement` 持久化「衰减陈旧」快照，永久重新强化已衰减事实

**位置**：`atom_memory/reinforce.py:725-735`

```python
if state.applied and state.gain > 0.0:
    n = state.n                    # ← 末次事件时刻的值，未衰减到重放时刻
    last = state.last_used_at
conn.execute("UPDATE facts SET reinforce_count = ?, last_used_at = ? ...", (n, last, seen, fact_id))
```

`state.n` 是**末次事件瞬间**的值，而增量写入路径（`record_reinforcement:642`）与所有读取方（`retriever.py:517`、`api.py:1620`）都在 `last_used_at` 基础上 `adjust()` 到当前。

**实测（lead 独立探针）**：事实在 `t0` 有 1 次强化，5 天后：

```
incremental adjust(n=1.0, last_used_at=t0, at=now) = 0.9548
rebuild returned n = 1.0000; persisted reinforce_count = 1.0000
persisted last_used_at == t0 ? True
--> CONFIRMED DIVERGENCE
```

**影响**：任何调用 rebuild 的维护/修复过程会**永久抹掉 5 天的衰减**；且该模块的核心不变式（`reinforce.py:668-675` 声称重放「精确复现增量路径结果」）**为假**，因此「靠重放重调曲线」不安全。

**建议修复**：UPDATE 前把最终状态衰减到重放时刻——`n = adjust(n, last, now_ms())`，并同步 `last_used_at = now_ms()`。

---

### B-5 facts 批量保存：把未编辑字段一并回写，且失败静默关闭弹窗丢弃用户编辑

**位置**：`dsh/src/client/memory-settings-controller.ts:398-421`，配合 `dsh/src/client/MemorySettingsSection.tsx:701-704`

```ts
for (const row of rows) {
  if (row.deleted) { await this.r().deleteFact({ user: USER, fact_id: row.fact_id }) }
  else {
    await this.r().editFact({
      user: USER, fact_id: row.fact_id,
      subject: row.subject, predicate: row.predicate, object: row.object,
      content: row.content,
      type: row.type,          // ← 弹窗从不渲染 type，值来自 state，默认 ''
    })
  }
}
await this.refreshData()
} catch (err) { /* 只写 lastError，不 rethrow */ }
```

Python 侧「非 None 即视为要设置该列」（`api.py:1531-1532`）：

```python
if type is not None:
    updates["type"] = clean_field(type, 64) or "semantic"    # '' -> 'semantic'
```

**两个叠加缺陷**：

1. **零编辑也改写所有事实的 type**。`type` 未被弹窗渲染，回写时默认 `''`，被 Python 归一为 `'semantic'`——破坏 `sop`/`lesson`/`decision_rule` 分类，并影响 `default_importance(memory_type)` 排序与 `durable` 归档保护（B-1 中的 `durable` 集合正读此列）。
2. **部分失败后静默关闭、丢失其余编辑**。`saveAllFacts` 吞异常不 rethrow（对比 `saveAllProfile:438` **明确 rethrow** 并注释说明原因：「A per-row loop could apply half the edits and then fail」）；弹窗 `.finally(() => { setSaving(false); onClose() })` 无条件关闭，`rows` 是弹窗本地 state，关闭即销毁。

**影响**：用户「全量保存」一次即静默损坏全部事实的类型标注；批量编辑中途失败则前几条已提交、其余丢失且无提示。

**建议修复**：
- 只发送**脏字段**；永不发送 `type`（或让弹窗真正渲染它）。
- 让 `saveAllFacts` 与 `saveAllProfile` 一致地 rethrow；弹窗改为 `.then(close).catch(保留编辑器 + 显示错误)`（照抄 `ProfileEditorModal:819-824` 的既有正确写法）。
- 后端提供批量 `edit_facts` 接口，避免 N 次串行 RPC。

---

## 三、建议项

### S-1 `domain.py:1431` + `:1438` —— 一个事实可出现多个 `is_primary`

```python
is_primary = 1 if label.is_primary or keep_primary else 0        # 1431
"is_primary = MAX(is_primary, excluded.is_primary), "            # 1438
```

主标签只升不降。**实测（lead 独立探针）**：先后 primary 到两个不同主题后，`fact_domain` 留下 **2 行 `is_primary=1`**：

```
BUG2 rows: [(1, 1), (2, 1)] -> primaries = [1, 2]
```

**影响**：`labels_of()` 返回两个 primary；排序与冲突判定读 primary，`ORDER BY is_primary DESC, confidence DESC` 的 tie-break 使事实主题可能随置信度静默翻转。
**修复**：同一事务内先 `UPDATE fact_domain SET is_primary = 0 WHERE fact_id = ? AND domain_id != ?`。

### S-2 `domain.py:1417` —— `domain_max_per_fact` 在单次调用内不生效

`existing` 是 `:1411` 的一次性快照，循环中新增的标签未计入。子代理实测 `domain_max_per_fact=3` 时单次 `attach()` 带 4 个标签**存入 4 行**。
**修复**：循环前截断 `assignment.labels[:limit]`，或维护 `added` 计数。

### S-3 `scope.py:1207-1212` —— `_promote_candidate` 的 UPDATE 过度匹配同族候选

```python
"UPDATE scope_candidate SET status = 'promoted' "
"WHERE user_id = ? AND scope_type = ? AND canonical_name = ?",
```

唯一键是 `(user_id, scope_type, canonical_name, signal_type, normalized_value)`（`011_init.sql:127`），而该 WHERE 省略了后两项。子代理实测：同名下 `folder_path` 与 `path` 两行 pending 候选，提升其一**把两行都标为 promoted**，静默退役一个从未达标的候选（其状态须为 `pending` 才会被 `_promote_matured:1154` 看到）。
**修复**：WHERE 补上 `AND signal_type = ? AND normalized_value = ?`。

### S-4 `reinforce.py:601` + `:656` —— 事件插入与聚合更新不在同一事务

插入事件与更新聚合各自 commit。若两者之间失败，UNIQUE 索引（这是幂等机制本身）会让重试看到 `rowcount == 0` 而返回 `None`，**聚合更新被永久跳过**——证据行存在但从未计数。
**修复**：两句包进同一个 `with conn:`。

### S-5 `retriever.py:679` —— `truncate_to_tokens` 可超出给定上限

省略号 `…`（1 个非 CJK 字符）在检查**之后**追加，未计入预算；当 `other` 处于 4 的倍数边界时恰好多出 1 token。

**实测（lead 独立探针）**：21 例越界，如 `limit=1 → 2 tokens`、`limit=2 → 3 tokens`。
**影响**：`max_fact_tokens` 是每条事实的上限，越界使其失效；`api.py:476` 还用 `max(ceiling - line_tokens, 1)` 复算 `t`，越界会传导到每事实上限判定。
**测试缺口**：`truncate_to_tokens` **零直接测试覆盖**（全仓 grep 无命中），现有 `test_hardening.py:302` 用的是数千 token 的长正文，1 token 越界不可见——这正是缺陷存活的原因。
**修复（lead 已验证消除全部越界）**：循环前预留省略号成本：

```python
ell = token_cost("…")[0]
budget = limit - ell
```

并补一条边界测试（小 limit × 混合中英文）。

### S-6 `worker.py:510` —— 退避 sleep 卡住整个队列

`await asyncio.sleep(delay)`（最长 60s）位于 `_handle_task` 内，由串行 `_run` 循环 await（`:345`）。一个失败任务会推迟**所有**其他待处理任务；注释（`:522`）所述「yield to the loop」的意图并未达成——drain 循环本身就是该 loop。租约 600s 时还可能把重试推过自己的租约。
**修复**：改为每任务 `not_before` 时间戳，由 claim 查询跳过未到点者。

### S-7 `api.py:343` —— `_write_receipt` 以 20ms 忙轮询占用事件循环线程

每次迭代都做**同步** SQLite SELECT（不 yield）。`wait_ms=5000` 即 250 次唤醒 + 250 次同步读，与 worker 争用同一连接。
**修复**：退避轮询（20ms → 200ms）或由 worker 主动通知。

### S-8 `capture.ts:108` —— 按会话的 `recent` Map 无淘汰，随进程生命周期增长

内层数组有上限（`maxRecent=20`），**外层 key 集合没有**，且全文件无删除路径（lead 已 grep 确认）。对照 `context.ts:215-218`（LRU 上限 200）与 `scope.ts:244-248`（`SIGNAL_CACHE_MAX`）均正确设界——`capture.ts` 是唯一的例外。
**修复**：外层 Map 设 LRU 上限（~200 会话），或在会话结束时清理。

### S-9 `capture.ts:152-155` + `index.ts:209-229` —— 超时写入被每次 sweep 无限重发为**新候选**

sweep 在 await **之前**置 `captured = true` 且失败不复位：瞬时失败永久丢弃该消息。反向而言，常规路径超时（默认 30s）会标 `failed`，此后每次 sweep 重发；`api.add` 每次生成新 `candidate_id`（`api.py:249`），TS 又从不传 `idempotency_key`（`worker.py:172` 仅在调用方提供时继承）——**每次重试 = 新候选 = 重复事实 + 额外 embedding + 额外 LLM 调用**。
**修复**：sweep 失败时复位 `failed` 并设尝试上限；用稳定幂等键（`capture.ts:165` 已算出 `seq` 却从未使用）。

### S-10 `rpc.py:418` 等 7 处 —— 线上参数用裸 `int()` 解析

`rpc.py` 中 7 处 `int(params.get(...))` 无保护（`:418, 465, 466, 491, 529, 531, 555`）。其中 `_summary`/`_changes`/`_overview_skeleton` 由 `_dispatch` 的**显式分支**处理，**绕过**了 `:255-257` 的 `TypeError → _RpcError("bad arguments...")` 包装；非数值输入会把 Python 内部 `ValueError` 字符串直接回给面板。
**影响**：跨语言数值边界未收敛。TS 侧当前传 `number` 故未触发，属**潜伏**问题。
**修复**：统一 `_int_param(params, key, default)` 兜底。

### S-11 `overview.py:336` + `:346` —— 为选 3 条而在内存中累积全部事实

`unit["_candidates"].append(fact)` 收集该单元**每一条** active 事实，之后才排序切片到 `highlights_per_unit`。子代理实测 5000 条单单元事实峰值堆 **4.3 MB**，而该模块文档（`:262-266`）自称「Every limit here exists to keep the result bounded」。
**修复**：累积时用 `heapq.nlargest` 对抗 `_fact_rank_key`，峰值降为 O(单元数 × 高亮数)。

---

## 四、风格项

| 位置 | 问题 |
| --- | --- |
| `retriever.py:631-632` | docstring 称「every five non-CJK characters」，而 `CHARS_PER_TOKEN = 4`（`:653`）——过期注释，与代码矛盾 |
| `context.py:371-376` | `normalize_signal` 的 `startswith("explicit_")` 分支与默认分支**返回完全相同**，死分支 |
| `context.py:395-399` | `canonical_name_for` 同理：两条路径都 `return normalized` |
| `sanitize.py:80` | `_ZERO_WIDTH` 为死常量（全文件仅定义处出现） |
| `sanitize.py:72` vs `:74` | `0xE0020` 同时出现在字面量列表与 `range(0xE0020, 0xE0080)`，重复 |
| `worker.py:1734` | `_enqueue` 无任何调用方（死代码），而 4 处插入各自手写 SQL 并硬编码 `max_retries` 字面量 |
| `worker.py:2147` | `retention_days is None` 分支不可达（参数类型为 `int`） |
| `api.py:825` | 用裸 `assert` 作控制流（`python -O` 下消失，退化为 AttributeError） |
| `tools.ts:1283-1288, 1392-1397` | `memory_scope`/`memory_domains` 的 list 动作仍宣传 `user` 参数，但 Python 侧不接收 |

---

## 五、已核实**无问题**（明确排除，防重复调查）

1. **任务领取 CAS 正确**：`worker.py:391-405` 的 select-then-update 带 `AND status='pending'` + `rowcount != 1` 判定。子代理用两条独立连接实测交错：A `rowcount=1`、B `rowcount=0`，无双执行。
2. **reclaim 优先级正确**：`worker.py:310-311` 混合 `OR`/`AND` 实际优先级无误——对端活跃租约保留、遗留 NULL 租约行回收、未来 `started_at` 不动。
3. **SQLite 线程安全未被破坏**：全仓未设 `check_same_thread`，但连接**只在事件循环线程**使用；embedding 经 `asyncio.to_thread` 走不碰 DB 句柄的路径。
4. **无事务跨越 await**：`api.py:1937` 的 `await to_thread(embed)` 正确位于 `with self.db:` **之前**；`backup.py:155-168` 亦把 embedding 预计算置于事务外（其 docstring `:133-141` 解释了为何必须如此）。
5. **失败路径先回滚再记账**：`worker.py:467` `_safe_rollback()` 在 `_finish_candidate`/`_record_failure` 之前，避免提交半成品索引。
6. **无灾难性正则回溯**：`sanitize.py`/`extractor.py` 全部模式在对抗输入（`'的'*30000`）下 < 1ms；`(.+?)` 惰性量词均有确定后继锚点。`_cap` 保证 `len(result) <= limit`。
7. **所有子进程输出 `JSON.parse` 均有 try/catch**（`bridge.ts:291-293, 310-316`）。
8. **所有定时器在 dispose 时清理**（`index.ts:270`、`capture.ts:195`、`overview.ts:331/346/354`）。
9. **Remote 契约完整**：`remote.ts` 的 17 个描述符与 `controller.ts` 的 `@Remote` 方法逐一对齐；`_METHODS` 分派表（`rpc.py:68-133`）与 `api.py` 方法名/签名全部对得上。
10. **`summary` 的 `overview: true` 映射正确**（非字段错名）：`controller.ts:201` → `rpc.py:405` → `AtomMem.summary(use_overview=...)`。
11. **`scope.ts:162` 的 `signalCache` 有界**（`SIGNAL_CACHE_MAX`，delete+set LRU），其文档声明属实；`context.ts:153` 的 `frozen` 同样有界（200，LRU）。
12. **迁移 011 的清库是一次性的**，012/013 均**明确声明不再重复**并遵守（`012_init.sql` / `013_init.sql` 均有专段说明）。

---

## 六、修复顺序建议

1. **B-1**（容量上限静默失效）——策略空转且无声，优先级最高。
2. **B-5**（UI 全量保存损坏类型 + 丢失编辑）——用户可直接触发的数据损坏。
3. **B-2**（detail 预算越界）——每次请求都付费，且随预算放大。
4. **B-4**（rebuild 抹掉衰减）——破坏模块核心不变式。
5. **B-3**（截断记录重复）——展示层错误，条件窄。
6. S-1/S-2/S-3（domain/scope 不变式）+ S-5（truncate 越界 + 补测试）。
7. 其余按性价比处理。

**注意**：B-3 修复时需同步补 `wait_ms > 0` 的测试；B-5 修复时需同步补 `saveAllFacts` 的测试（当前**零覆盖**，这正是缺陷存活的原因）；S-5 修复时需补 `truncate_to_tokens` 的边界测试（当前**零覆盖**）。

---

## 七、审查方法与其局限

- **lead 独立复现**：B-1、B-2、B-3、B-4、B-5、S-1、S-5 均由 lead 用独立探针实跑确认，未采信子代理结论。探针脚本已全部删除，仓库工作区除本报告外无改动。
- **修正了一处子代理结论**：B-3 在 `api.add` 常规路径上两列表**按字段互斥**，并非每次都重复；真实触发条件为 `wait_ms > 0` 且两阶段截断同一字段。已在该条注明。
- **验证了失败测试的影响面**：B-5 的 `type='' → 'semantic'` 经 `api.py:1531-1532` 确认（`clean_field('') or "semantic"`）。
- **未做的**：未对 UI 做真实浏览器端到端点击验证（B-5 的 UI 半边依据源码与控制器对照）；未压测 S-11 的大规模内存表现（采信子代理 4.3MB/5000 条实测）；未做并发压力测试（B-1/S-6/S-7 为静态+单线程探测结论）。