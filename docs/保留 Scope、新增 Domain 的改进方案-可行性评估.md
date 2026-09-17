# 《保留 Scope、新增 Domain 的改进方案》可行性评估

评估对象：`docs/保留 Scope、新增 Domain 的改进方案.md`（下称“本方案”）
评估依据：本仓库实际代码（`atom_memory/`、`dsh/src/`、`atom_memory/migrations/`）与既有设计文档 `docs/scopes.md`、`docs/memory-semantics.md`。
评估日期：以工作区当前 HEAD 为准。

**文档结构**：第一部分是**评估记录**（针对原文档的问题清单，保留原样以留痕）；第二部分是**可定稿版设计**（以修订版为基线，全部为决议，含分期实施与验收）。两处冲突时以第二部分为准。

---

---

# 第一部分：评估记录

## 一、总体结论

**方向可行，工程量大，且有 4 处会导致返工的结构性缺口。**

| 维度 | 结论 |
| --- | --- |
| 问题定义 | 成立。跨领域干扰、训练集/项目错配导致的“召回为空”，是 scope 维度天然解决不了的一类问题 |
| 数据模型 | 基本可行，但 `domains` / `domain_signals` 缺 `user_id`，与 `facts.user_id` 隔离模型冲突 |
| 写入路径 | 可行且改动集中。`.ptmp` 上的候选 dict 直接扩展即可，无需改事务边界 |
| 召回路径 | **主要缺口**。查询推断 domain 的落点错误；分层降级与“先 top-N 再过滤”的查询计划互相矛盾；无条件的 scope MUST 会压制本方案的核心目标 |
| 冲突/指纹/快照 | 设计自洽，且与现有机制（`content_fingerprint`、`fact_scope`、冻结快照）能对上 |
| 迁移 | 第六章与第十六章自相矛盾（“不保留现有数据” vs “离线回填 + 人工审核”） |
| 建议 | 保留设计意图，按第二部分重排落点后分三期实施 |

规模判断：新增约 3 张表、1 个 store 模块、召回链路重构 + TS 侧 4 个文件、Python 侧 6 个文件；按现有测试密度（`tests/` 约 20 个文件、`dsh/tests/` 约 17 个文件）需同步新增至少 `test_domain.py` / `test_domain_writes.py` / `test_domain_recall.py` 三个测试面。属于“1~2 周量级的特性”，不是补丁。

---

## 二、必须先修的结构性缺口

### 缺口 1：查询推断 domain 的落点与现有架构冲突（高）

第十一章写“在查询预处理中用轻量调用”推断 domain。但本仓库的 LLM 能力**只在 dsh 宿主侧**：
`atom_memory/rpc.py:289-302` 明确拒绝跨线传 `llm_extractor`（"llm_extractor cannot be sent over the wire; extraction runs in the dsh host"），抽取提示词与模型调用都在 `dsh/src/llm-extractor.ts`。

因此“召回时 LLM 推断 domain”只有两条路：

1. 在 `dsh` 侧推断（把 query → domain_hints 与 `scope_context` 一起下发）——**推荐**，符合现有分层，且 TS 侧已有 `buildLlmCompleter`（`dsh/src/llm-extractor.ts:198`）可复用；
2. 在 Python 侧开一条新的 LLM 回调通道——与 rpc.py 的既有决定直接冲突，且 `Retriever.search` 目前是纯本地计算（一次 `embed_one` + 两条 SQL）。

若按本方案在 Python 侧做，等于推翻 `rpc.py` 里那条注释与拒绝逻辑，需要一并改文档与测试。**这一条不修，第 6.3 / 6.8 / 11 章无法落地。**

### 缺口 2：分层召回与查询计划互斥（高）

第 6.8 节要求“五层逐级放宽、结果不足自动进入下一层”，第 13 章却要求“先用 FTS5 取 top-N，再用 scope 和 domain 过滤”。

两者不能同时成立：一旦过滤发生在 top-N 之后，候选池就已经固定，放宽只能靠**再跑一遍检索**。而当前 `scope_sql` 是放进 `facts_vec` 的 `fact_id IN (subquery)` 与 FTS 的 JOIN 条件里的（`retriever.py:400-404`、`445-453`），是**检索阶段过滤**，不是排序阶段。所以：

- 五层 = 最多 5 次 FTS + 5 次 KNN + 5 次 RRF 融合；每次 KNN 都要 `embed_one`（有缓存则复用）；
- 现有的 `AND (EXISTS ... OR EXISTS ...)` 谓词本身已经通过 `IN (subquery)` 在 KNN 内部生效，再加一层 `fact_domains` 的 EXISTS，等于给 vec0 的过滤子查询再加一个相关子查询，**延迟随候选宽度线性增长**；
- 第 13 章担心的“EXISTS 拖慢 FTS5”在现有代码里**已经存在**，不是本方案引入的新问题，本方案是把它放大。

建议：把“层”实现为**一次检索 + 多档谓词**（同一候选池，先按 must 谓词过滤，不足则用放宽谓词重跑），并把“每层最小结果数”做成显式的短路上限（例如最多降级 2 层），否则召回 p95 会明显劣化。

### 缺口 3：scope 无条件 MUST 会压制本方案的核心卖点（高）

第 6.5 节写“scope 始终是 MUST”，第 6.8 节第一层也是“scope 严格 + domain 严格”。但本方案要解决的问题 2（“在教学项目里问写 Python 脚本，要召回编程记忆”）恰恰是**跨上下文**的。

现有候选集是 `ScopeView.visible_ids()` = 自身 + 祖先 + 本项目的其他 phase（`scope.py:1898-1910`），兄弟项目的事实**完全不进候选**，只能通过 `fact_condition` 的条件匹配进来（`validator.py:_scope_clause`、`retriever.py:_scope_predicate` 的第二个 OR 分支），而条件项权重只有 0.15（`config.py:177`）。

按现有权重算一次：在项目 scope 内、但需要靠条件匹配进来的跨领域事实，
- 最乐观：`0.4·1.0 + 0.2·1.0 + 0.2·1.0 + 0.2·1.0 + 0.15·1.0 + 0.20·0.15(其他 scope) = 1.18`
- 同 scope 但 relevance 一般（0.5）的事实：`0.4·0.5 + 0.2+0.2+0.2 + 0.20·1.0 = 1.0`

**结论：跨领域事实只有在 scope 内结果不足、触发降级到第 4/5 层时才会真正出现。** 这不是缺陷，而是必须写明的语义——“domain 让跨领域记忆**可被发现**，而不是让它**默认参与竞争**”。本方案第 6.10 节“domain 不是会话常量”是对的，但第 6.8 节把 scope 放在每一层前面，实际效果是 scope 仍然是准常量。建议在第 6.8 节明确：**第 4 层必须是常态可达的降级路径，不是异常兜底**，否则目标 2 落空。

### 缺口 4：`domains` / `domain_signals` 缺 `user_id`，与隔离模型冲突（中高）

`facts.user_id` 是硬隔离边界（`retriever.py` 的每条查询都带 `user_id`，`rpc.py` 的 `forget_all` 按 user 走）。而第 3.1 节的 `domains`（canonical 唯一）与 `domain_signals`（按建议名称 + scope 计数）都是**全局表**，没有 `user_id`：

- 多用户部署下，A 用户的 "teaching/ds" 与 B 用户的 "teaching/ds" 会共享 `domain_id`，`fact_domains` 的关联本身仍按 fact 隔离，不泄漏内容，但**信号计数、审核队列、注册状态会被串味**；
- `scope_candidate` 的既有做法是带 `user_id`（`011_init.sql:113`、唯一索引 `user_id, scope_type, canonical_name, signal_type, normalized_value`）。domain 应照抄这一条，而不是新开一个例外。

修法：`domains` 加 `user_id`（或明确“domain 是 deployment 级词表”并在文档里承认这一取舍，同时把 `domain_signals` 至少按 user 分桶）。

---

## 三、中等风险与不完善之处

1. **迁移自相矛盾（第 12 章 vs 第 16.5 节）**：一边“不保留现有数据”，一边安排“离线回填现有 facts、人工审核 signals”。domain 与 scope 一样是**隐含属性**——“Python 脚本”这条事实里没有任何字段说明它属于 programming。回填只能靠重新跑抽取/分类，代价与重新积累相当。建议二选一：要么照 `011_init.sql` 的先例清库（并说明这是一次性代价），要么明确回填只是**可选离线工具**且不保证准确率。

2. **`primary_domain` 与截断的次序 bug（第 5.1 / 5.4 节）**：校验链先“最多三个，超过截断”，再校验“primary 必须在集合中，否则取第一个”。若 LLM 把 primary 放第 4 位，截断后 primary 丢失，只能退化为“第一个”，与 LLM 的意图相反。应**先选 primary，再截断其余**。

3. **指纹合并 domain 的语义（第 8.2 节）**：“把新 domain 加入 `fact_domains`”会把一条事实的 domain 集单调增长。反复在不同上下文复述同一句通用的话，最终会得到一条 domain = {teaching, programming, life, ...} 的“万能事实”，而第 7.1 节的冲突规则又是“domain 有交集才冲突”——交集越宽，判定越钝。建议：`fact_domains` 带 `confidence`/`first_seen`（本方案已有置信度字段，应明确**合并 = 取 max 而非并集增长**），并在 domain 集超过上限（比如 5 个）时按置信度裁剪。

4. **排序公式与现有权重约定冲突（第 6.7 节）**：本方案要求“其他项按比例缩放，保持总和为一”。但现有实现的约定是**scope 的三个项是加在四个基础项之外的**（`retriever.py:604-608`，文档 `docs/scopes.md:189-191` 也明确写了），四基础项之和为 1（`config.py:232 weights_sum()`），加项后满分约 1.4。再插一个 domain 项并要求总和归一，会让“同一事实在 scoped / scope-blind 两种模式下得分含义一致”这条既有保证失效（`retriever.py:489-494` 的注释）。建议：**domain 项照 scope 的做法直接追加**，不改归一约定；文档不要提“保持总和为一”。

5. **must 模式下的候选池塌缩**：FTS 的 `LIMIT k` 与 vec0 的 `LIMIT k` 都是在过滤后的集合上取前 k。加 domain 过滤后，两侧可能**大幅低于 k**，融合后为 0 而“其实有结果只是被过滤掉了”。若走 must，必须显式把 `k` 放大为 `k × 倍数`（本方案第 13 章提了“N 为 k 的数倍”，但没把它和 must 模式绑定），并把“filtered-to-empty”和“true-empty”在 `last_degraded` / 返回值上区分开——现有代码已经有 `last_degraded` 与 `last_scope` 两个诊断通道（`retriever.py:197-201`），应复用它。

6. **快照冻结的落点**：第 9.3 节要“会话开始时冻结 scope + domain 快照”。现有冻结由 dsh 侧在 `dsh/src/context.ts:169` 完成（携带 `scope_context`），`atom_memory/summary.py:_render_scoped` 只是按本次 payload 渲染。domain 若进快照，需要 dsh 在冻结时把 domain 集合一并写进 payload；这是**唯一的自然落点**，不要在 Python 侧缓存会话态（`MemConfig` 是无状态配置，Python 侧没有可靠的 session 生命周期钩子）。

7. **`_BLOCK_SHARE_*` 预算需重算（第 9.2 节）**：知识体按 domain 分组 = 多出若干块。现有每块份额是 `current 0.5 / ancestor 0.25 / global 0.3 / phase 0.25 / condition 0.25`（`summary.py:165-169`，故意 oversubscribe 再由 `_BLOCK_SHRINK_PASSES` 逐次减半）。再加 domain 分组块，会使“全局规则始终包含”的保留份额被更快吃掉。要么把 domain 分组**只用在 detail 深度**（`summary.py` 的 detail 是 drill-down，不受预算约束），要么给 domain 块一个低于 0.2 的份额并把它排在收缩顺序最前。

8. **半自动打标的“事后修正”缺工具（第 5.7 / 11 章）**：本方案承诺“用户可事后修正、非阻塞提示”，但第十一章只列了 `memory_domains`（列出可用 domain），**没有 `fact_domain_set` / `fact_domain_get`**。参照 scope 的成熟做法（`fact_scope_bind` / `fact_scope_get` / `memory_scope` 的 merge/split/reparent），domain 至少要补齐 `domain_rename` / `domain_merge`（风险表里承诺了“重命名或合并只重写 fact_domains”，但没有对应 API）、`fact_domain_set`。**这是本方案最容易漏掉的一块工作量。**

9. **配置面**：`MemConfig` 是 dataclass（`config.py:22`），新增十几项 `domain_*` 配置会让 `MemConfig(**params)` 的启动校验面变大（`rpc.py:303`）。`domain_bridges` 是嵌套结构，与现有扁平标量配置风格不一致，建议用逗号分隔字符串或单独的配置读取函数，避免 `MemConfig` 变成混合类型容器。

10. **测试与灰度未见安排**：第十六章有实施顺序但没有测试/灰度方案。建议明确“domain_recall=off 时必须与当前行为逐字节一致”（现有 `scope_aware=False` / 无 `scope_context` 时保持了 pre-scope 的 byte-identical 排序，这是本仓库的一条硬约定，见 `retriever.py:489` 与 `config.py:174-175`），并把它写成回归测试。

---

## 四、方案中判断正确、可以直接采纳的部分

- **第 2.1 / 2.2 节“正交不替代、scope 单一归属、domain 多标签”**：与现有 `fact_scope`（多对多、`priority` 破平）同构，落地没有阻抗。
- **第 3.3 节“不把 domain 塞进 facts 表”**：理由成立，尤其是“不破坏现有 facts 写入路径”。
- **第 5.5 节“不引入新事务边界”**：正确且必须。`_persist_fact` 目前在同事务内写 facts + FTS + vec（`worker.py:1026+`），`fact_domains` 追加到同一事务是唯一安全的做法。
- **第 7 章冲突域 = scope + SPO + domain 交集**：与 `validator._check_conflict` / `cross_scope_neighbours` 的两段式判定能对上；`domain 无交集不冲突` 正是给“同 scope 跨领域”补的一条口子，方向对。
- **第 8.1 节“指纹不含 domain”**：正确，且与 `fingerprint.py` 的文档一致（“描述观察的属性不进身份”）；`negated` 都进了身份，domain 这种更弱的观察属性更不该进。
- **第 8.3 节“跨 scope 指纹匹配不自动合并”**：与现有“同 scope 才是 dedup/冲突窗口、跨 scope 只连 `cross_scope_similar`”的决定完全一致（`docs/scopes.md:165-169`）。
- **第 9.1 节“compact 剥离 domain 前缀”**：与 `_render_scoped` 的既有取向一致，且节省预算的理由成立。
- **第 10 章画像不动**：正确，`user_profile` 是用户自有的表，不该被新维度拖下水。

---

## 五、建议的落地顺序（修订版）

**第一期：只做数据模型 + 写入打标，召回完全不变（可独立上线）**

1. `012_init.sql`：建 `domain` / `fact_domain` / `domain_signal`（统一单数表名，且 `domain` / `domain_signal` 带 `user_id`；参照 `scope` 的索引风格：`parent_id`、`canonical_name` 唯一、`fact_domain(domain_id, fact_id)`）。
2. `MemConfig` 加少量开关，`domain_tagging_mode` 默认 `auto`；`domain_recall` 默认 `off`。
3. 候选链路扩展：`FactCandidate` 加 `domain_hints` / `primary_domain`（`models.py:73`）；`_candidate_from_rpc_dict` 解析（`worker.py:105`）；`llm-extractor.ts` 提示词加 canonical 列表与约束（第 5.1 节）。
4. 写入校验链（第 5.4 节，修正截断次序）+ 同事务写 `fact_domain`。
5. `memory_add` 返回渲染 domain（第 5.6 节）。
6. 管理面：`memory_domains`（list/resolve/create/rename/merge）、`fact_domain_get`/`fact_domain_set`——**与 scope 的 8 个方法对齐**。

验收：`domain_recall=off` 时全部现有测试通过且排序逐字节不变；新增 `test_domain.py` / `test_domain_writes.py`。

**第二期：召回接入，只有 should 一档**

7. `Retriever.search` 接受 `domain_ids` + `mode`；`facts_vec` 与 FTS 的谓词增加 `fact_domain` 分支（先只做 should 加权，不硬过滤 —— 硬过滤才是性能与空结果风险的来源）。
8. 排序项按 scope 的既有约定**追加**，不改归一。
9. `dsh/src/bridge.ts` / `tools.ts` 注入 `domain_ids`；`context.ts` 冻结时写 domain 集合。
10. 查询推断放在 **dsh 侧**（复用 `buildLlmCompleter`），失败/无模型时退化为关键词规则 → 会话默认。

验收：目标 1（主题隔离：教学查询不混入旅游）与目标 2（跨领域可召回）各写成端到端用例；对同一查询比较 `off / should` 两种模式的 top-k 差异。

**第三期：must、分层降级、桥接、多意图拆分**

11. 只有在第二期实测“should 加权不足以排除干扰”后，才引入 must 与分层；分层实现为**一次检索 + 多档谓词的最多 N 次重跑**，并记录 `retrieval_level`（第 6.8 节）+ 降级事件（第 6.9 节）。
12. 桥接与多意图拆分最后做（第 6.4 节），它们是调优项而非成立前提。

---

## 六、给方案作者的最小修改清单

| 处 | 改什么 |
| --- | --- |
| 3.1 | `domains` / `domain_signals` 补 `user_id`，并说明是否 deployment 级词表 |
| 5.1 / 5.4 | domain 校验链先定 primary 再截断 |
| 6.3 / 11 | 查询推断落到 dsh 侧；写明 Python 侧不接受 LLM 回调（引用 `rpc.py` 的既有决定） |
| 6.5 / 6.8 | 承认 scope MUST 会挡住跨领域事实；把第 4 层定义为常态路径；给出触发条件 |
| 6.7 | 删掉“保持总和为一”，改为“按 scope 的既有约定追加一项” |
| 8.2 | domain 合并改为置信度取 max + 上限裁剪，而非单调并集 |
| 9.2 | domain 分组限定在 detail 深度，或给低于 0.2 的块份额 |
| 12 / 16.5 | **已被第二部分决议 3 取代**：不清库，靠“未打标 = general”兼容子句 + 灰度 |
| 11 | 补 `fact_domain_set/get`、`domain_rename/merge` → 第二部分第五节 |
| 13 | 把“top-N 再过滤”和“分层”两句话对齐；must 模式下显式放大 N；区分 filtered-to-empty 与 true-empty → 第二部分 2.3 / 2.4 |
| 新增 | 测试与灰度章节；`domain_recall=off` 的 byte-identical 回归要求 → 第二部分第六节 |

> 本节（第一部分）是**评估记录**：它记录的是修订前原文档的问题清单。第二部分的决议已吸收其中的全部条目；两处冲突时以第二部分为准。

---

# 第二部分：可定稿版设计

以下内容以修订版（"保留 scope 作为上下文隔离层，新增 domain 作为主题分类层；写入半自动打标，召回硬过滤，冲突按交集判断，迁移灰度推进"）为基线。**凡第一部分列出、此处未重复的条目，按修订版原文执行**——修订版已经解决了排序项归一、查询推断落点、domain 不参与 MUST 排序三件事。

本部分的全部内容都是**决议**，不再是建议：每条都给出默认值，实现时不需要再回来问。

## 一、三条决议

### 决议 1：Scope 单一归属的强制执行（与"domain 多标签"配对，不可只做一半）

`fact_scope` 的既有 schema 是多对多（`011_init.sql:132`），但修订版第 2 条要求"事实必须属于一个 scope"。因此写入路径需要新增一条校验：**`_write_scope_ids()` 返回的绑定中，`bind_scope_id` 必须存在且唯一**；`replace` 继承（`worker.py:1340-1347`）、`fact_scope_bind` 多绑（`api.py:818`）、merge/split（`scope.py:1515/1642`）都要一并收紧为"主 scope + 可选附加绑定"。

现状是"事实可绑多个 scope，`fact_scope.priority` 破平"（`011_init.sql:135`），不收紧的话"单一归属"只是文档里的一句话。**这是修订版里唯一一条现有代码与之冲突的原则**，必须显式改。

### 决议 2：domain 归属到 user，不是 deployment 词表

`domain` / `fact_domain` 表带 `user_id`（或经 `facts` 间接隔离 + `domain` 带 `user_id`）；`domain_signal` **必须**带 `user_id`，并按 `(user_id, canonical_name, scope_id)` 做唯一键——照抄 `scope_candidate` 的做法（`011_init.sql:113`、`127-129`）。

理由：内容不会泄漏（`fact_domain` 经 `facts.user_id` 隔离），但信号计数与审核队列会串味；`scope_candidate` 已经确立了"排队类数据按 user 分桶"的先例，domain 不应新开例外。

### 决议 3：未打标事实 = `general`，在 must 过滤下可见

**这是 domain 维度的兼容子句，对应 scope 的"无 `fact_scope` 行 = 全局事实"**（`011_init.sql:31-35`、`retriever.py:351`）。

```
没有 fact_domain 行  →  视为 general  →  must 过滤下可见，should 模式下匹配度为 0
```

没有这条，`off → should → must` 切到 must 的那一刻，所有未回填的历史事实会集体消失，而回填永远不可能 100%。有了它，**清库不再是必须的**：回填不完整只影响主题排序质量，不造成事实丢失。迁移因此简化为：建表 → 写入打标 → 灰度切换 → 回填（可长期进行），不需要 `011` 式的清库，也不需要"人工审核全部 signals 后才敢开过滤"。

## 二、召回：四层阶梯（取代修订版第 4 节的四级列表）

修订版原文说"FTS5 和向量检索都先按 scope、domain 过滤，再融合"——这与现状的实现方式一致，落地无障碍（`scope_ids` 现在就是通过 `facts_vec` 的 `fact_id IN (子查询)` 与 FTS 的 JOIN 条件生效的，`retriever.py:400-453`）。但**"一次检索 + 事后标记"无法实现四层**：过滤一旦在 `LIMIT` 之前，候选池就固定了。因此四级必须是**顺序执行 + 短路**的四次（最多）检索。

### 2.1 每一层的谓词

| 层 | scope 谓词 | domain 谓词 | 标记 | 触发进入的条件 |
| --- | --- | --- | --- | --- |
| L1 | 自身 + 祖先 + 本项目其他 phase（`ScopeView.visible_ids()`；**现状不含"后代"**，见下） | MUST：当前 domain + 祖先 ∪ 未打标 | 当前上下文 | 结果数 `< n_min` |
| L2 | 同上 | SHOULD：不硬过滤，匹配 domain 加权 | 同主题扩展 | 结果数 `< n_min` |
| L3 | 同上 | 按桥接表放宽为 SHOULD，桥接命中项降权 | 跨领域参考 | 结果数 `< n_min` 且启用桥接 |
| L4 | 放宽到祖先 + 兄弟 scope | off | 跨上下文参考 | 结果数 `< n_min` |

> **一处需要与修订版对齐的现状**：修订版第 4 节写 `scope_ids = 当前 scope + 祖先 + 后代`，但现有 `ScopeView.visible_ids()` 返回的是**自身 + 祖先 + 本项目的其他 phase**（`scope.py:1898-1910`），**不含后代 scope**——后代只作为 `_extra` 参与打分，不进入候选集。`docs/scopes.md:177` 的表述与此一致。若要按修订版加入"后代"，需要显式改 `visible_ids()`，并注意它同时改变现有的候选集语义（子文档/子 phase 的事实会进入每一次 recall）。**默认按现状执行（不含后代）**，把"加入后代"作为独立议题。

**`n_min` 默认值：`max(1, ceil(0.6 × k))`，不是常数。** 常数在 `k=10` 时会让 L1 几乎永远不饱和、L2 永远执行；在 `k=3` 时又会让 L1 一次就通过、四级形同虚设。按 `k` 的比例定义才能随调用方预算伸缩。配置项名：`domain_layer_min_ratio`，默认 `0.6`。

**短路语义**：某一层结果数达到 `n_min` 即停止降级，返回该层结果 + 更早层结果（去重后按 `final_score` 合并）。**不允许每层都跑满再合并**——那是 4 倍召回成本换一点边际收益。

### 2.2 "兄弟 domain"不再定义（修订版第 4 节第 3 层的歧义，就地消除）

修订版第 4 节第 3 层写"兄弟 domain，标记跨领域参考"，但没有定义兄弟集。按 `parent_id` 遍历时，根 domain 之间**没有共同父**：要么 `general` 是共同父（于是所有根 domain 互为兄弟，第 3 层退化为"不过滤"，与硬过滤自相矛盾），要么不是（于是第 3 层没有集合可算）。两条路都不通。

**决议：删除"domain 树上的兄弟"这一概念。** 跨领域可达性只由两个东西提供：

1. **桥接表**（`domain_bridge`，显式配置的非对称/对称关系，如 `teaching ↔ programming`）。桥接只影响**排序权重**（`W_BRIDGE`，默认 0.6 × SHOULD 权重），不产生新的过滤集合；
2. **L2 / L3 / L4 的逐级放宽**。

"主题隔离"于是来自 MUST（L1），"跨领域可用"来自桥接加权（L3）与 scope 放宽（L4）。**这两者不再互相矛盾**——修订版里它们矛盾，是因为第 3 层被寄望于用一个未定义的集合同时承担两者。

### 2.3 `retrieval_level` 与"过滤空 vs 真空"

每层结果带 `retrieval_level`（1–4）；返回值与 `events` 里必须区分：

- **`filtered`**：本层谓词过滤后候选数为 0，但放宽一层就有结果 → 是过滤造成的，不是没数据；
- **`empty`**：所有层都为空 → 真没有。

这个区分复用现有的两个诊断通道（`retriever.last_degraded` / `last_scope`，`retriever.py:197-201`），不要新开通道。降级事件写 `events`（按修订版第 5 节），字段：`query`、会话 domain、推断 domain、每层尝试与结果数、最终层、`filtered|empty`。

### 2.4 MUST 模式下的候选塌缩与 relevance ceiling（必须处理）

FTS 的 `LIMIT ?` 与 vec0 的 `LIMIT ?` 都作用在过滤**之后**（`retriever.py:409/451`）。MUST 模式下可用候选可能远小于 `k`，且：

- 融合后可能为空，而"空"与"被过滤掉"在代码里区分不开 —— 由 2.3 解决；
- **绝对化 relevance 被扭曲**：`relevance = rrf / rrf_ceiling`，`rrf_ceiling = 2/(k_rrf+1)` 是"rank 0 in both lists"的固定值（`retriever.py:143-152`）。若 MUST 把高排名候选滤掉，第一名在 FTS 里可能是 rank 3，relevance 被压到约 0.5 以下，一个本该满分的命中看起来平庸，并直接对抗 `min_relevance` 门限。

**决议（二选一，实现时按此执行）**：

- 方案 A（推荐，保持既有"绝对分数"哲学）：MUST/SHOULD 模式下按**过滤后各列表的实际可用秩**计算 ceiling 修正，即用本层实际进入融合的两列表长度归一；
- 方案 B（更简单，代价是失去跨模式可比性）：过滤模式下 relevance 退回按本层候选池的相对秩计算，并在文档与代码注释里写明"scoped+domain 模式下 relevance 不是绝对量"。

同时在 MUST 模式下**把两边的 `k` 放大**（`k × domain_recall_overfetch`，默认 `3`）再取，别直接用 `k`。

## 三、召回：查询 domain 的来源与落点

**落点（决议）：dsh 侧推断，不从 Python 侧调 LLM。** `atom_memory/rpc.py:289-302` 明确拒绝跨线传 `llm_extractor`；在 store 内做 LLM 推断等于推翻该决定，且会让 `Retriever.search` 从纯本地计算变成有外部依赖。可复用 `dsh/src/llm-extractor.ts:198` 的 `buildLlmCompleter`。

**三级来源 + 优先级**：

1. **查询推断**（dsh 侧轻量调用，返回 domain + 置信度）：优先于会话默认；
2. **会话默认**（scope 映射、显式标签、cwd/git remote、兜底 `general`）：作为初始候选与 L1 的 MUST 集合；
3. **规则兜底**（关键词映射）：模型不可用或超时时的降级路径。

**MUST 的准入条件（决议）**：只有当"查询推断出单一 domain、置信度 ≥ `domain_must_threshold`（默认 0.8）、且与会话默认不冲突"时，L1 才用 MUST；否则 L1 直接使用 SHOULD。**这是硬过滤不伤害目标 2 的唯一出口**——"在教学项目里问写 Python 脚本"这种查询，推断出的 domain 与会话默认冲突，于是 L1 从 MUST 降为 SHOULD，编程记忆在第一层就参与竞争，而不是等 L4。

## 四、写入：与召回对称的三条补充

1. **校验次序修正**：先选 `primary_domain`，再截断到 3 个其余 domain（原方案第 5.1/5.4 节的次序会让 LLM 放在第 4 位的 primary 被截断掉）。
2. **domain 合并取 max，不取并集**（原方案第 8.2 节）：重复复述同一句话若把 domain 单调并集，会得到 domain = {teaching, programming, life, ...} 的"万能事实"，而冲突判定又是"有交集即冲突"，交集越宽判定越钝。**决议**：`fact_domain.confidence = max(旧, 新)`；单条事实的 domain 数上限 `domain_max_per_fact`（默认 5），超出按 confidence 裁剪；`primary_domain` 只在新 domain 置信度显著更高（≥ 0.1）时才替换。
3. **写入回退与会话默认同源**：写入时"LLM 没给或给错 → 回退会话默认 domain 集合"必须与召回时 `scope → domain` 的映射用**同一份代码**（参照 `resolution_for` 作为 scope 唯一入口的设计，`scope.py:1944`）。两份映射实现在写侧和读侧各写一遍，就是"事实写在 A 域、读在 B 域"的经典 bug。

## 五、接口与配置（补齐修订版未列的部分）

**RPC / 工具面**（与 scope 的 8 个方法对齐，`api.py:658-880`）：

```
memory_domains  list / resolve / create / alias_add / confirm / merge   ← 模型可见
fact_domain_set(fact_id, domains)      fact_domain_get(fact_id)         ← 事后修正
domain_signal_list(user_id)            domain_signal_reject(user_id, id) ← 审核队列
```

风险表承诺"重命名或合并只重写 `fact_domains`"——那就必须有 `rename` / `merge`，否则承诺无法兑现。

**`retriever.search()` 新增参数**：`domain_ids`、`domain_mode`（`must|should|off`）、`domain_layers`（bool）。`bridge.ts` / `tools.ts` 在构造 recall 请求时注入（`dsh/src/tools.ts:279-292` 已有同类注入点）。

**配置项（`MemConfig`，扁平标量，`config.py:146` 一节）**：`domain_recall`（默认 `off`）、`domain_must_threshold`（0.8）、`domain_layer_min_ratio`（0.6）、`domain_recall_overfetch`（3）、`domain_max_per_fact`（5）、`domain_tagging_mode`（`auto`）、`domain_bridges`（逗号分隔字符串，**不要用嵌套结构**——`rpc.py:303` 的 `MemConfig(**params)` 需要扁平可解析）。

`domain_bridge` 若需要权重就用 `domain_a:domain_b:weight,...` 的单字符串编码，或单独一张配置读取函数；不要让 `MemConfig` 变成混合类型容器。

## 六、分期实施（含验收）

**一期：模型 + 写入打标，召回**完全**不变（可独立上线）**

- `012_init.sql`：`domain`（带 `user_id`、`parent_id`、canonical 唯一）、`fact_domain`（带 `confidence`、`is_primary`）、`domain_signal`（带 `user_id`）、`domain_bridge`；索引照 `scope` 的风格。
- `FactCandidate` 加 `domain_hints` / `primary_domain`（`models.py:73`）；`_candidate_from_rpc_dict` 解析（`worker.py:105`）；`llm-extractor.ts` 提示词加 canonical 列表与"最多 3 个、primary 必须在内、不确定留空"。
- 写入校验链 + 同事务写 `fact_domain`（不引入新事务边界）。
- `memory_add` 返回渲染"写入到哪个 scope、哪些 domain、哪些被回退"。
- 管理面全套（第五节的接口）。

**验收**：`domain_recall=off` 时全部现有测试通过且排序**逐字节不变**（现有代码在 `scope_aware=False` / 无 `scope_context` 时保持 pre-scope 的 byte-identical 排序，`retriever.py:489-494` + `config.py:174-175`，这是本仓库的硬约定）；新增 `test_domain.py` / `test_domain_writes.py`。

**二期：召回接入，只有 SHOULD 一档**

- `search()` 接受 `domain_ids` / `domain_mode`；FTS 与 vec0 谓词加 `fact_domain` 分支（含"未打标 = general"兼容子句）。
- SHOULD 加权项照 scope 的既有约定**追加**在四项之外，不改归一。
- dsh 侧：`context.ts` 冻结时写入会话 domain（唯一自然落点，`dsh/src/context.ts:169`）；`bridge.ts` / `tools.ts` 注入。
- 查询推断放 dsh 侧，模型不可用时走规则兜底。

**验收**：目标 1（教学查询不混入旅游）与目标 2（教学 scope 内召回编程记忆）各一条端到端用例；同一查询比较 `off / should` 的 top-k 差异。

**三期：MUST + 四层阶梯**

- 先做 2.4 的候选放大与 relevance 处理，再做 2.1 的阶梯；`retrieval_level` 与 `filtered|empty` 一并落地。
- MUST 准入条件（第三节）与桥接（2.2）同期。
- 灰度：`off → should → must`，每档观察 L1 命中率与降级率，`filtered` 比例异常升高即回退。因为决议 3 的兼容子句，"未回填"不会造成事实消失，回退随时可做。

**四期（可延后）：摘要分组与回填**

- compact 剥离 domain 前缀（省预算）；知识体按 domain 分组**只用在 detail 深度**，或给低于 0.2 的块份额（现有 `_BLOCK_SHARE_*` 之和已故意 oversubscribe，`summary.py:165-169`，再加分组块会更快吃掉"全局规则"的保留份额）。
- 离线回填作为**可选工具**，明确不保证准确率，且不阻塞灰度（决议 3）。

## 七、定稿后仍需作者拍板的两件事

1. **未打标事实在 SHOULD 模式下的匹配度**：本文档定为 0（即 `general` 只在 MUST 下作为"可见"处理，不获得加权）。若希望未打标事实在 SHOULD 下也拿到部分权重（例如 0.3），需要作者确认——这会影响灰度期的召回质量，但不影响任何架构。
2. **`domain` 是否参与 `promote_abstractions`**（跨 scope 提升为全局规则，`scope.py:1736`）：建议**参与**——"三个不同 scope 独立持有同一主张"如果再要求"domain 也相同"，会把"同一件事在不同项目被反复确认"这一最强的通用性证据排除掉。默认按"参与、domain 取交集作为提示但不作为门槛"实现。

## 八、落地清单（按文件，供直接开工）

**一期（模型 + 写入）**

| 文件 | 改动 |
| --- | --- |
| `atom_memory/migrations/012_init.sql` | 新建 `domain`（`user_id` / `parent_id` / `canonical_name` 唯一 / `confidence` / `status` / `created_at`）、`fact_domain`（`fact_id` / `domain_id` / `confidence` / `is_primary`，PK `(fact_id, domain_id)`）、`domain_signal`（`user_id` / `canonical_name` / `scope_id` / `seen_count` / `status`，唯一键含 `user_id`）、`domain_bridge`（`a` / `b` / `weight`）；索引照 `scope` 风格；插入 `general` 根 domain（硬编码 id，便于祖先遍历，照 `scope.id=1` 的先例）。**不清库** |
| `atom_memory/domain.py`（新） | `DomainStore`：注册制、`resolve_or_fallback`（读写共用的唯一映射入口）、祖先遍历、bridge 查询、`fact_domains(ids)` 批量读（照 `ScopeStore.fact_scopes`）、`set_fact_domains`（confidence 取 max + 上限裁剪）、`merge` / `rename`。树缓存 + 写入失效 |
| `atom_memory/models.py` | `FactCandidate` 加 `domain_hints: tuple` / `primary_domain: Optional[str]` |
| `atom_memory/worker.py` | `_candidate_from_rpc_dict` 解析两个新字段；新增 `_validate_domains`（先选 primary 再截断到 3）；`_persist_fact` 同一事务写 `fact_domain`；决议 1 的"主 scope 唯一"校验加在 `_write_scope_ids` / `_apply_candidates` |
| `atom_memory/config.py` | `domain_tagging_mode` / `domain_max_per_fact` / `domain_bridges`（扁平字符串）+ 二期三项，默认 `domain_recall="off"` |
| `dsh/src/llm-extractor.ts` | 提示词加 canonical 列表与约束（最多 3 个、primary 必须在内、不确定留空）；candidate 类型加字段 |
| `dsh/src/tools.ts` | `memory_add` 返回渲染 scope + domain + 回退提示；新增 `memory_domains`、`fact_domain_set/get` |
| `atom_memory/api.py` / `rpc.py` | 上述方法透出（照 `scope_*` 的既有排布） |
| `tests/test_domain.py`、`tests/test_domain_writes.py`（新） | 注册制、祖先回退、primary 截断次序、confidence 取 max、上限裁剪、user 隔离、无 LLM 时的规则回退 |

**二期（SHOULD 召回）**

| 文件 | 改动 |
| --- | --- |
| `atom_memory/scope.py` 或 `domain.py` | 会话 domain 集合的解析（`scope → domain` 映射 + 显式标签 + 兜底 `general`），与写侧共用同一函数 |
| `atom_memory/retriever.py` | `search()` 加 `domain_ids` / `domain_mode`；`_scope_predicate` 旁加 `_domain_predicate`（**含"未打标 = general"兼容子句**）；`_rerank` 追加 SHOULD 加权项 |
| `dsh/src/context.ts` / `bridge.ts` / `tools.ts` | 冻结时写会话 domain；每次 recall 注入 `domain_ids` + `domain_mode`；dsh 侧查询推断（复用 `buildLlmCompleter`） |
| `dsh/tests/*` | 注入路径与 mode 传递的单测 |

**三期（MUST + 四层）**

| 文件 | 改动 |
| --- | --- |
| `atom_memory/retriever.py` | `domain_recall_overfetch` 放大 `k`；MUST 下 relevance ceiling 处理（2.4 方案 A）；四层阶梯 + 短路 + `retrieval_level`；`filtered` / `empty` 区分复用 `last_degraded` |
| `atom_memory/db.py` / `worker.py` | 降级事件写 `events` |
| `tests/test_domain_recall.py`（新） | `off` 模式 byte-identical 回归；目标 1 / 目标 2 端到端；四级短路与 `filtered` 标记 |
