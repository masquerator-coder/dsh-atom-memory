# Domain 维度落地方案（全新数据库基线）

**前提：不保留历史数据。** 本方案以"清空事实库、全新开始"为基线设计，因此不需要给旧数据留兼容通道，可以把上一版设计里那些"为兼容而存在"的分支全部收紧成数据库级约束。

与《保留 Scope、新增 Domain 的改进方案》及其可行性评估的关系：本文件是**落地版**，取代评估文档第二部分中依赖"未打标 = general"兼容子句的那些条目。凡本文件未提及的，沿用评估文档第二部分的决议。

核心变化一句话：**旧基线里 domain 是"打得越全越好"的加分项，新基线里 domain 是写入的必备字段，由数据库强制。**

---

## 一、三条铁律

### 铁律 1：每条活跃事实恰好一个主 scope，至少一个 domain

不变量清单（全部由 schema + 写入路径共同保证）：

| # | 不变量 | 保证方式 |
| --- | --- | --- |
| I1 | 活跃事实**恰好一个**主 scope | `fact_scope.priority = 0` 的唯一主绑定；写入路径 `_write_scope_ids()` 只返回一个 id；`fact_scope_bind` 的多绑只允许 `priority = 1` 的附加绑定 |
| I2 | 活跃事实**至少一个** domain，**恰好一个** `is_primary = 1` | `CHECK` 约束在 fact 层面无法表达，由写入路径 + 一条校验查询保证；测试覆盖 |
| I3 | 指纹不含 domain | `content_fingerprint()` 输入不变（`fingerprint.py:58`） |
| I4 | 同一用户 + 同一指纹 + 同一 scope 至多一条活跃事实 | 现有 dedup 语义不变 |
| I5 | domain 树按 `parent_id` 遍历，`path` 只是标签 | 照抄 `scope` 的既有决定（`docs/scopes.md:52`） |
| I6 | 跨 scope 的事实不互为冲突，跨 domain 的也不 | `validator._check_conflict` 的 scope 窗口 + domain 交集判定 |
| I7 | 召回时 scope 与 domain 都是检索阶段硬条件 | 谓词进 FTS JOIN 与 vec0 的 `fact_id IN (子查询)` |

I1 和 I2 是这一版相对上一版**新增**的强约束。它们之所以在"清库"前提下可执行，是因为：

- 旧库里存在"无 `fact_scope` 行 = 全局事实"的历史包袱（`011_init.sql:31-35`），新库没有；
- 旧库里 domain 语义上可以是空的，新库里空 domain 是**写入失败**，不是"落 general"。

### 铁律 2：一切自动归属必须可解释、可修改、不弹窗

写入静默、事后可改、结果回执里写明"依据"。用户看到 `memory_add` 的返回就知道这条记忆属于哪个 scope、哪些 domain、以及**为什么**（`hint` / `scoped_map` / `keyword` / `session_default`），改起来是一条命令。

### 铁律 3：过滤对"少召回"负责，不对"多召回"负责

MUST 过滤能保证"教学查询里不出现旅游记忆"，**不能**保证"教学记忆一定会被召回"。后者由 scope 可见性决定（见第四节）。这条写进文档是为了避免落地后把"没召回出来"误判成 domain 的问题——**大多数情况下那是 scope 的问题**。

---

## 二、数据模型（`012_init.sql`）

表名单数，与 `scope` / `fact_scope` 的既有风格一致；`path` 标签化，遍历只靠 `parent_id`。

```sql
-- domain: 主题词表，按 user 隔离（照 scope_candidate 的先例）
CREATE TABLE domain (
    id             INTEGER PRIMARY KEY,
    user_id        TEXT NOT NULL,
    canonical_name TEXT NOT NULL,            -- 小写 ASCII，路径式：teaching/ds/ch3
    display_name   TEXT NOT NULL,            -- 中文显示名
    parent_id      INTEGER REFERENCES domain(id),
    path           TEXT NOT NULL,            -- 展示用，不参与遍历
    status         TEXT NOT NULL DEFAULT 'active',
    merged_into    INTEGER REFERENCES domain(id),
    system_seeded  INTEGER NOT NULL DEFAULT 0,  -- 1 = 系统给的通用根，不允许删除
    created_at     INTEGER NOT NULL,
    last_seen_at   INTEGER NOT NULL,
    CHECK (status IN ('active','merged','archived'))
);
CREATE UNIQUE INDEX idx_domain_unique ON domain(user_id, canonical_name);
CREATE INDEX idx_domain_parent ON domain(user_id, parent_id);
CREATE INDEX idx_domain_path   ON domain(user_id, path);

-- fact_domain: 事实与 domain 的多对多（与 scope 的单一归属形成对照）
CREATE TABLE fact_domain (
    fact_id     TEXT NOT NULL REFERENCES facts(fact_id) ON DELETE CASCADE,
    domain_id   INTEGER NOT NULL REFERENCES domain(id) ON DELETE CASCADE,
    confidence  REAL NOT NULL DEFAULT 0.5,
    is_primary  INTEGER NOT NULL DEFAULT 0,
    source      TEXT NOT NULL,                -- hint / scoped_map / keyword / session_default / user_explicit
    created_at  INTEGER NOT NULL,
    PRIMARY KEY (fact_id, domain_id)
);
CREATE INDEX idx_fact_domain_domain ON fact_domain(domain_id, fact_id);
CREATE INDEX idx_fact_domain_fact   ON fact_domain(fact_id);

-- domain_bridge: 跨领域桥接，只影响 should 权重，不产生过滤集合
CREATE TABLE domain_bridge (
    user_id   TEXT NOT NULL,
    from_id   INTEGER NOT NULL REFERENCES domain(id) ON DELETE CASCADE,
    to_id     INTEGER NOT NULL REFERENCES domain(id) ON DELETE CASCADE,
    weight    REAL NOT NULL DEFAULT 0.5,
    PRIMARY KEY (user_id, from_id, to_id)
);

-- domain_signal: 未注册 domain 的观察队列（注册制的入口）
CREATE TABLE domain_signal (
    id               INTEGER PRIMARY KEY,
    user_id          TEXT NOT NULL,
    canonical_name   TEXT NOT NULL,
    nearest_ancestor INTEGER REFERENCES domain(id),  -- 建议被并入的已注册祖先
    scope_id         INTEGER REFERENCES scope(id),
    seen_count       INTEGER NOT NULL DEFAULT 1,
    status           TEXT NOT NULL DEFAULT 'pending',
    first_seen       INTEGER NOT NULL,
    last_seen        INTEGER NOT NULL,
    CHECK (status IN ('pending','promoted','rejected'))
);
CREATE UNIQUE INDEX idx_domain_signal_unique
    ON domain_signal(user_id, canonical_name, scope_id);
CREATE INDEX idx_domain_signal_pending ON domain_signal(user_id, status, seen_count);
```

**`fact_domain.source` 是本方案的一等功能字段，不是调试信息。** 它是"半自动"可解释性的载体：回执、管理界面、以及第四节的准确率观测都读它。

**`system_seeded`**：系统预置的根 domain（`general` / `teaching` / `programming` / `life`）不允许被 `domain_delete`，只允许 `archive` 或 `merge`，避免用户误删导致 `general` 兜底消失。

**`domain` 的 id 不用硬编码根 id。** `scope` 当年把根硬编码成 `id = 1` 是为了祖先遍历能走捷径（`011_init.sql:37-39`）；domain 不必复制这个技巧——每用户一个 `general` 根，按 `(user_id, canonical_name)` 查一次并缓存即可。

---

## 三、domain 解析：单一入口

```python
# atom_memory/domain.py
class DomainStore:
    def resolve_hints(self, user_id, hints, session_domains) -> DomainAssignment: ...
    def resolve_or_fallback(self, user_id, *, scope_path, hints, text, session) -> DomainAssignment: ...
    def ancestors(self, domain_id) -> list[int]: ...
    def view(self, user_id, domain_ids, mode) -> DomainView: ...
```

### 3.1 优先级（四级，全部有 `source` 标记）

| 级 | 来源 | `source` | 适用 |
| --- | --- | --- | --- |
| 1 | 用户显式指定（`memory_add` 的 `domain_hints` 参数） | `user_explicit` | 最高，不被覆盖 |
| 2 | LLM 抽取的 `domain_hints` / `primary_domain`，校验通过 | `hint` | 默认路径 |
| 3 | **scope → domain 映射表**（按 `scope.path` 前缀匹配） | `scoped_map` | 兜底主力 |
| 4 | 关键词规则 | `keyword` | 模型不可用时的降级 |
| 5 | 会话默认 domain（含祖先链） | `session_default` | 最后兜底，永远存在 |

**第 3 级是这一版的重点。** 上一版让"LLM 没给就落 `general`"，那是灾难性的——`general` 会变成一个巨大的、每次召回都必须放行的桶，隔离能力直接归零。在清库前提下，**"LLM 没给"的正确处理是用 scope 推断**：正在教学项目里写入的东西，主题大概率是 teaching。

映射表由两部分组成：

```python
# config 里的静态前缀表（deployment 预置）
scope_domain_map: tuple = (
    ("/global/project:courseware",  "teaching"),
    ("/global/project:ds-course",   "teaching/ds"),
    ("/global/project:api-tool",    "programming"),
)

# 用户在 domain 管理面维护的动态映射（存 domain 表旁的一张 map 表或配置）
```

关键性质：**映射的粒度和类型**。`project:courseware` 是项目级，所以这门课下的所有工作（备课、讲义、练习、评分）默认都是 `teaching`——除非 LLM 明确说了别的（比如"这节课要讲 Python 的装饰器" → `teaching` + `programming` 双标签）。

### 3.2 校验（写入时）

1. 每个 hint 先精确匹配 `canonical_name`，再逐级向上找已注册祖先（`teaching/ds/ch3` 未注册 → 若 `teaching/ds` 已注册则并用之，且把未注册的 `canonical_name` 记入 `domain_signal`）。
2. **先选 `primary_domain`，再截断其余**，上限 3 个（`domain_max_per_hint`）。原方案把次序写反了：先截断会让 LLM 放在第 4 位的 primary 被丢掉。
3. `primary` 不在最终集合中时，取**置信度最高**的一个，而不是"第一个"。
4. 去重（同一事实的 hint 可能解析到同一已注册祖先）。
5. 集合为空 → 走 3.1 的第 3/4/5 级。**不会为空**：`session_default` 必有值。

### 3.3 注册制与初始词表

**初始词表不静态预置，而是在第一次会话时按 scope 生成**（这正是清库带来的好处）：

```
首次在某工作目录启动
  → 解析 scope（复用 ScopeStore.resolve）
  → 若 scope 是 project/document/thread 级，取显示名生成 domain 建议（teaching/ds/ch3）
  → 自动注册到 nearest 已注册祖先（通常是 general）
  → 建立 domain_bridge（同一 user 下）
```

用户可导航，也可在 `memory_domains` 里改。`domain_signal` 只在"LLM 建议的 domain 无法解析到已注册祖先"且"scope 也推不出来"时使用——**注册制没有被取消，但它的触发频率会比原方案低得多**，因为 scope 映射承担了大部分归属。

---

## 四、Scope 归属：这一版真正的风险点

domain 硬过滤只负责"不混入"，能不能"召回得到"由 scope 可见性决定。现有可见性规则（`scope.py:357-419`、`1898-1910`）：

```
候选集 = 自身 + 祖先 + 根 + 本项目的 phase 后代
        非 phase 后代（document / thread）不进候选
        兄弟 scope 只能靠 condition 匹配进入（权重 0.15）
```

两条对本场景致命的推论：

1. **被绑到 `document` scope 的教学事实，在另一个 document 里召回不到**（`scope.py:374-375` 明确如此设计："a document-scoped fact is not visible from its sibling documents just because they share a project"）。
2. **绑在 `user` 级的通用偏好可见；绑在兄弟 `project` 上的不可见。**

因此本方案对 scope 侧提出三条**必须一起做**的配套动作：

### 4.1 抬高 `document` / `thread` 的创建门槛

`document` 的合法信号是 `doc_id`(0.90) / `share_link`(0.75) / `doc_title`(0.50) / `path`(0.50)（`docs/scopes.md:72-79`）。课件、讲义这类工作极易命中 `path` / `doc_title`，于是被钉在 document 级。

**决议**：`document` / `thread` scope 的创建阈值单独配置（`scope_new_threshold_document`，默认 0.85），`path` / `doc_title` 这类弱信号**只能**绑定到已存在的 document，不能创建它。项目内的教学事实因此默认落在 `project` 级，跨章节可见。

> **已实施**：`config.py:scope_new_threshold_document` + `scope.py:ScopeStore._creation_threshold`（按 scope 类型取阈值）。只对 `document` / `thread` 生效——其他层级继续用单一通用阈值，因为一个从裸 `path`（0.50）建出来的 project 仍然是可达的（它的 phase 与 document 后代都能看到它）。

### 4.2 "提升到祖先 scope"成为一等操作

`promote_abstractions` 现在只在"三个不同 scope 独立持有同一主张"时自动触发（`scope.py:1736`，阈值 `scope_abstraction_min_scopes = 3`），对"我知道这条是通用的"这种人类判断太迟钝。

**决议**：提供 `fact_scope_promote(fact_id, to_scope_id)`（或 `memory_scope` 的 `promote` action），把一条事实的主 scope 提升到 `user` 或 `project` 级；`fact_scope` 主绑定改写，`fact_domain` 不动。这是用户修正"这条教学偏好应该对我所有项目生效"的唯一低成本手段。

> **已实施**：`ScopeStore.promote_fact` → `AtomMem.fact_scope_promote` → RPC `fact_scope_promote` → `memory_scope(action='promote')`。两条设计约束落在代码里：目标必须**更通用**（`SCOPE_DEPTH` 比较，只能提升不能下埋），旧主绑定降为 `priority = 1` 而非删除（可逆，且事实仍以"曾是某文档"的身份可读）。写 `events.fact_scope_promoted`。

### 4.3 教学类"通用偏好"的默认归属 = `user` scope + `teaching` domain

`docs/memory-*` 的既有设计里，用户级偏好本来就该落 `user` scope（`011_init.sql` 的 scope 类型表里有 `user`）。**教学偏好（讲概念再举例、每章配三个练习）属于这一类**：它对"这门课"和"下一门课"都成立。

**决议**：抽取提示词对"偏好 / 属性"类事实默认建议 `scope_hint = user`，而"这门课的具体安排"仍落 project。这条改动很小，但它决定了"下一门课能不能直接受益于上一门课的积累"。

> **部分实施，后半段有意推迟**：提示词已按上述要求改写（`dsh/src/llm-extractor.ts`）。Python 侧新增 `context.RESERVED_SCOPE_HINTS = {global, user}`：这两个值是**层级标记**而不是地名，被识别后丢弃，绝不当作名字去建 scope——否则"每个会话建一个叫 user 的项目"就是一个必然的退化。**把这类事实真正落到 `user` scope 的动作推迟到 domain 维度之后**：把一个偏好提升到"处处可见"的层级，正是主题污染重新进入的路径，而 domain 硬过滤才是它的解药。在此之前，偏好仍写在会话所在 scope。

---

## 五、写入路径

```
dsh 侧（llm-extractor.ts）
  ├─ 提示词新增：domain canonical 列表（来自当前 user 的活跃 domain，最多 ~40 个）
  │   规则：最多 3 个；primary 必须在内；不确定就留空
  └─ candidate 增加 domain_hints / primary_domain
        ↓ persist_candidates（RPC）
Python 侧（worker.py）
  ├─ _candidate_from_rpc_dict 解析两个新字段
  ├─ _validate_domains（DomainStore.resolve_hints + 校验链）
  ├─ _resolve_write_scope（既有）
  └─ 单一事务：
        facts + facts_fts + facts_vec + fact_scope(1 行) + fact_domain(1..3 行)
        ↓
     回执（memory_add 返回）
        scope: /global/project:courseware · 依据 git_remote
        domain: teaching/ds/ch3(主) · teaching
        source: hint · hint
```

### 5.1 写入失败而非静默降级

I2 意味着"没有 domain 的事实不能存在"。因此：

- `resolve_or_fallback` 返回空集合时（理论上不可能，因为 `session_default` 必有值），**写入失败并记 `events`**，而不是"落 general"；
- 但 `session_default` 的兜底链保证这条不会触发：`scope → domain` 映射 → 关键词 → 该 user 的 `general` 根。**`general` 只作为最终兜底，不作为默认值。**

这个区别是整个方案成立与否的分水岭：`general` 是兜底，意味着它很少出现；`general` 是默认，意味着它是最大的桶，MUST 过滤形同虚设。

### 5.2 幂等与 domain 合并

指纹命中已有事实（`_fold_into`，`worker.py:1198`）时：

- `fact_domain` **取 max 而非并集**：`confidence = max(旧, 新)`，`source` 取更高优先级者；
- 单条事实 domain 上限 `domain_max_per_fact`（默认 5），超出按 confidence 裁剪，`is_primary` 永不被裁掉；
- `is_primary` 只在新 domain 置信度显著更高（差 ≥ 0.1）时替换。

理由见评估文档：并集增长会让反复复述的一句话变成 `domain = {teaching, programming, life, ...}` 的"万能事实"，而冲突判定又是"有交集即冲突"，交集越宽判定越钝。

### 5.3 冲突域

```
冲突域 = scope（相同）+ subject + predicate + object + domain 交集（非空）
```

- 不同 scope → 不冲突（既有语义）；
- 同 scope、domain 无交集 → 不冲突，记为 `cross_domain_similar`（对应既有的 `cross_scope_similar`，`scope.py:1371`）；
- 同 scope、domain 有交集 → 走既有冲突策略（`conflict.py` 不变）。

---

## 六、召回路径

### 6.1 三档模式

| 模式 | 语义 | 默认启用条件 |
| --- | --- | --- |
| `off` | 完全不过滤、不加权 | 配置为 off，或 domain 树未建立 |
| `should` | 不硬过滤，domain 匹配加权 | 意图模糊、推断置信度低 |
| `must` | 候选必须属于有效 domain 集合 | 意图明确（见 6.3） |

### 6.2 谓词（进检索阶段，与 scope 并列）

```sql
AND ( EXISTS (SELECT 1 FROM fact_domain fd
              WHERE fd.fact_id = f.fact_id
                AND fd.domain_id IN (<有效 domain 集合>))
      OR <should 模式：恒真；must 模式：无此分支> )
```

**没有"未打标放行"分支**——这是清库换来的简化。每条事实必有 domain，谓词可以干净地二分。

**MUST 下放大取数**：`k × domain_recall_overfetch`（默认 3）后再取，因为 FTS 的 `LIMIT` 与 vec0 的 `LIMIT` 都作用在过滤之后（`retriever.py:409/451`），不放大就会拿到"被过滤空"，而不是"没有"。

**MUST 下的 relevance 修正**：`relevance = rrf / rrf_ceiling` 的 ceiling 是"rank 0 in both lists"的固定值（`retriever.py:143-152`）。滤掉高排名候选后，第一名可能落在 rank 3，relevance 被压到 0.5 以下并直接对抗 `min_relevance`。按过滤后两列表的实际可用长度归一（评估文档 2.4 的方案 A）。

### 6.3 会话 domain 与查询推断

**会话 domain**（dsh 侧解析，`context.ts` 冻结时写入）：
```
scope.path 查 scope_domain_map → domain + 祖先链 → 兜底 general
```
**会话 domain 是 L1 的初始集合，不是最终集合。**

**查询推断**（dsh 侧轻量调用，复用 `llm-extractor.ts:198` 的 `buildLlmCompleter`；Python 侧不接受 LLM 回调，`rpc.py:289` 已定此规矩）：

- 返回推断 domain + 置信度；
- **MUST 准入三条件**：推断出单一 domain、置信度 ≥ `domain_must_threshold`（默认 0.8）、与会话 domain 不冲突；
- 否则 L1 直接用 `should`。

这条准入条件解决了"教学项目里问写 Python 脚本"：推断出 `programming`、与会话 `teaching` 冲突 → 不满足 MUST → L1 就是 `should` → 编程记忆在第一层就参与竞争，而不是等降级。

### 6.4 三层阶梯（顺序执行 + 短路）

| 层 | scope | domain | 标记 | 进入条件 |
| --- | --- | --- | --- | --- |
| L1 | 自身 + 祖先 + 根 + 本项目 phase（`visible_ids()`） | `must`（不满足准入则 `should`） | 当前上下文 | 结果数 `< n_min` |
| L2 | 同上 | `should` + `domain_bridge` 加权 | 同主题 / 跨领域参考 | 结果数 `< n_min` |
| L3 | 放宽到祖先 + 兄弟 scope | `off` | 跨上下文参考 | — |

`n_min = max(1, ceil(domain_layer_min_ratio × k))`，`domain_layer_min_ratio` 默认 0.6（**不设常数**：`k=10` 时常数会让 L1 永不饱和）。

清库让阶梯从四层减到三层：原 L3「兄弟 domain」依赖一个无法定义的集合（根 domain 之间没有共同父），已由 `domain_bridge` 取代；原 L4「全局」在本方案里由 L3 的"放宽到祖先 + 兄弟 scope"覆盖。

**短路**：达到 `n_min` 即停，返回该层 + 更早层结果去重合并。不允许每层跑满——那是 3 倍召回成本换边际收益。

**`filtered` vs `empty`**：本层过滤后为 0 但放宽后有结果 → `filtered`；全部层为空 → `empty`。复用 `retriever.last_degraded` / `last_scope` 两个既有诊断通道（`retriever.py:197-201`），降级写 `events`。

### 6.5 排序

**domain 在 `must` 下不参与排序**（已是硬条件）；`should` 下追加一项，照 scope 的既有约定**加在四项之外，不重新归一**（`retriever.py:604-608`、`docs/scopes.md:189-191` 的既有决定）。

`should` 权重：主 domain 命中 1.0，次 domain 0.7，桥接 0.5，未命中 0。

---

## 七、可观测性（清库方案下的新增能力）

没有历史数据 = 没有旧行为要兼容，**可以直接建评测集**。这是这一版最大的隐性收益：把"LLM 打标准不准"从担忧变成可测的数字。

| 指标 | 定义 | 采集点 | 目标 |
| --- | --- | --- | --- |
| domain 打标准确率 | 人工标注 100–200 条候选的期望 domain，与系统输出比 | 离线脚本 | 主 domain ≥ 85%，集合召回 ≥ 90% |
| `general` 占比 | 落 `general` 的事实比例 | `fact_domain` 聚合 | **< 10%**（超过说明 scope 映射覆盖不足） |
| `source` 分布 | hint / scoped_map / keyword / session_default | `fact_domain.source` | `session_default` 应低于 20% |
| L1 命中率 | L1 达到 `n_min` 的查询比例 | 降级事件 | 提升即回归 |
| `filtered` 比例 | 因 domain 过滤为空的比例 | 降级事件 | **异常升高即告警**（是打标错或映射缺） |
| 目标 1 / 目标 2 用例 | 教学查询不含旅游 / 教学 scope 内召回编程 | 端到端测试 | 恒定通过 |

**`general` 占比是最灵敏的健康度指标**：它升高意味着 scope 映射没覆盖到某类工作目录，而不是 domain 机制有问题。

---

## 八、迁移：清库但要显式

**不要写成"自动清空"。** `011_init.sql` 当年自动清了库，并留下注释"Future migrations must never repeat this"（`011_init.sql:51-52`）。本方案遵守这条：**012 自己不清数据，而是拒绝在有旧数据的库上启动。**

```
启动时：
  若 facts 表存在且行数 > 0 且 domain 表不存在
    → 拒绝启动，报错：
      "This build requires a fresh fact store (domain dimension).
       Back up <db_path>, then run: dsh-atom-memory reset --fresh
       or delete the database file. Facts cannot be preserved:
       domain is not back-fillable."
```

`reset --fresh` 明确要求用户确认，并**先做一次 `backup`**（`api.py:1507` 已有该能力）再清。清空的是**事实及其派生物**：`facts` 及其级联（`fact_scope` / `fact_condition` / `fact_origin` / `fact_evolution`，`011_init.sql:132-166`）、`facts_fts` / `facts_vec`、`fact_reinforcements`、`fact_candidates`、以及 `task_queue` 里的 `extract`/`persist_pre`/`replace` 任务（`011_init.sql:191-193` 的既有清单）。实现上直接复用 `AtomMem.purge(user_id, fact_ids=None)`（`api.py:1694`）而不是手写 DELETE 清单——它已经把"事实及其索引与派生物"的清理收敛在一处，手写会漏。

`user_profile` 与 `scope` 树**保留**：它们不是从事实派生的（`011_init.sql:50-52` 的既有理由）；保留 scope 树还有一个直接好处——第二节的初始 domain 词表可以按已有 scope 生成，用户不用从零重建上下文。

**为什么拒绝启动而不是静默清空**：清库是用户的决定，不是迁移的副作用。一个"升级后记忆全没了"的静默行为，比任何设计缺陷都更容易让人放弃这个工具。

---

## 九、配置项

一期实际落地的形态（与设计稿有两处偏差，均为实现时的修正）：

```
# domain —— 一期已实现
domain_recall: str = "off"             # 一期只打标不过滤；三期的目标值是 "should"
domain_tagging_mode: str = "auto"      # off / auto（confirm_new 尚未实现）
domain_max_per_hint: int = 3           # ✅ 生效
domain_max_per_fact: int = 5           # ✅ 生效（超限跳过新增，不做淘汰）
domain_should_weight: float = 0.15     # 三期用
domain_primary_weight: float = 1.0     # 新增：DomainView 主主题权重
domain_secondary_weight: float = 0.7   # 新增：次主题/祖先/桥接基准权重
domain_must_threshold: float = 0.8     # 三期用
scope_domain_map: tuple = ()           # ((scope_path_prefix, domain_name), ...)
domain_keywords: tuple = ()            # 新增：((keyword, domain_name), ...) 规则兜底

# domain —— 三期（尚未实现）
domain_layer_min_ratio: float = 0.6
domain_recall_overfetch: int = 3

# scope（二期已实现）
scope_new_threshold_document: float = 0.85
```

两处与原设计的偏差：

- **`domain_recall` 默认 `off`（原稿写 `should`）**：一期承诺"写入打标、召回完全不变"。默认 `should` 会让升级后立刻改变排序，而标签质量在真实语料上还没有被量过——先用 `off` 上线，标签可以先看、先改，再谈让它影响召回。
- **`domain_bridges` 配置项被 `domain_bridge_add` 取代**：桥接是**数据**（每对主题一个权重，还要能读取、合并、跟随改名），塞进一个逗号分隔字符串会让"改一条桥接"变成"重写整个配置字符串"。表 `domain_bridge` 已经有它需要的一切。`scope_domain_map` / `domain_keywords` 保留为元组配置，因为它们是**部署级规则**，本来就该在配置文件里。

全部扁平标量或元组，`MemConfig(**params)` 可直接解析（`rpc.py:303`）。

> 二期的 scope 配套已随本次改动落地：`scope_new_threshold_document`、`ScopeStore.promote_fact` / `fact_scope_promote`（RPC + `memory_scope promote`）、`context.RESERVED_SCOPE_HINTS`。上表其余项仍是一期～四期待做。

---

## 十、分期与验收

### 一期：模型 + 初始词表 + 写入（可独立上线，召回不变）——**已实施**

1. ✅ `012_init.sql`：`domain` / `fact_domain` / `domain_signal` / `domain_bridge` 四张表 + 索引 + `CHECK`。两处实现细节值得记住：`domain_signal` 的唯一键建在**生成列** `scope_key = COALESCE(scope_id, -1)` 上（SQLite 把 UNIQUE 索引里的 NULL 视为互不相同，直接建在可空的 `scope_id` 上会让每次出现都新增一行而不是累加计数）；`domain.path` 存的是**规范名本身**（规范名本来就是一条从根出发的完整路径，再前缀父路径会得到 `teaching/teaching/ds`，让前缀匹配指向错误节点）。`db.py:SCHEMA_VERSION` 提到 12。
2. ✅ `atom_memory/domain.py`：`DomainStore`（`create` / `find` / `ancestors` / `chain` / `resolve_chain` / `session_domains` / `assign` / `attach` / `set_fact_domains` / `rename` / `merge` / `archive` / `set_bridge` / `record_signal` / `unresolved` / `seed_from_scopes` / `ensure_root`）+ `DomainView`（为三期准备的加权视图）+ `DomainAssignment` / `DomainLabel` / `SessionDomains` 视图类型。
3. ✅ 词表初始化：`AtomMem._seed_domains()` 在 `start()` 时按已有 scope 树生成（`seed_from_scopes`），并保证每个已知用户都有 `general` 根。词表取自 scope 的 `canonical_name` **列**而不是解析 `path` —— 规范名本身常含分隔符（`github.com/acme/api`），按 `/` 切会退化成 basename，让两个同名项目塌成一个主题。
4. ✅ `FactCandidate` 加 `domain_hints` / `primary_domain`；`_candidate_from_rpc_dict` 解析；`_resolve_write_domains` 做批量解析 + 校验（`_ordered_hints` 保证**先定主主题再截断**）。
5. ✅ `_persist_fact` 在**同一事务**里写 `fact_domain`（含 `source` 与 `is_primary`）；幂等折叠（`_fold_into` 路径）改为按 `confidence = max` 合并标签而不是丢弃。
6. ✅ 回执：`empty_outcome` 的 `domains` 键 + `dsh/src/tools.ts` 的 `renderPlacement`（作用域 + 主题 + 未注册提示）。管理面 `memory_domains`（list / resolve / create / rename / merge / archive / bridge_add / unresolved / signal_reject / fact_set / fact_get）+ RPC 12 个方法 + `AtomMem` 同名方法。

**验收**：`tests/test_domain.py`（44 项：命名规范、注册制、祖先回退、session 映射、队列计数、标签合并取 max、上限裁剪、merge/rename 不改事实、桥接只加权、种子词表）+ `tests/test_domain_writes.py`（16 项：写入打标与 `source`、同事务、`general` 只作兜底、未注册建议回退 + 入队、**primary 在截断前定位**、复述合并标签、`domain_tagging_mode=off` 不写任何表、scope 映射打标、公共面与 RPC 可达性、启动种子）。`domain_recall` 默认 `off`，全部既有测试通过（Python 侧 471 passed, 1 skipped）。

**未做（属于三期）**：`_domain_predicate`（召回过滤）、`DomainView` 尚未被 `Retriever` 使用、查询推断与 MUST 准入、分层降级。`domain_max_per_hint` 已生效，但 `domain_max_per_fact` 的**裁剪**只在 `assign` 的集合层面生效，`attach` 遇到超限时是**跳过新增**而不是淘汰最弱项。

### 二期：scope 配套（4.1 / 4.2 / 4.3）——**已实施**

7. ✅ `scope_new_threshold_document`（默认 0.85）：弱信号（`folder_path` / `doc_title` 0.50）不得创建 document，但**仍可绑定**到已存在的 document。实现在 `scope.py:ScopeStore._creation_threshold`（按 scope 类型取阈值），`config.py` 新增开关。
8. ✅ `fact_scope_promote`：`ScopeStore.promote_fact` + `AtomMem.fact_scope_promote` + RPC `fact_scope_promote` + `memory_scope` 的 `promote` action。规则：目标必须**比当前主 scope 更通用**（只能提升不能下埋），旧主绑定降为 `priority = 1` 而非删除，写 `events` 的 `fact_scope_promoted`。
9. ✅ 抽取提示词（`dsh/src/llm-extractor.ts`）对"关于用户自己"的durable preference / stable attribute 要求 `scope_hint = "user"` 或留空，明确禁止把当前项目名写上去。
   ⚠️ **只做了"认标记"，没做"据此改归属"**：`context.py:RESERVED_SCOPE_HINTS` 让 `user` / `global` 这两个**层级标记**被识别并丢弃，绝不当作名字去建 scope（否则每个会话都会建一个叫 "user" 的项目）。真正把这类事实落到 `user` scope 需要 domain 维度先存在——否则"所有偏好都提升到全局可见"正是编程偏好污染教学工作的路径。这是**有意为之的推迟**，不是遗漏。

**验收**：`tests/test_scope.py::test_a_weak_signal_may_not_create_a_document`、`test_a_durable_document_id_still_creates_a_document`、`test_a_weak_document_signal_binds_to_a_document_that_already_exists`、`test_a_level_marker_is_never_turned_into_a_scope_name`；`tests/test_scope_writes.py::test_promoting_a_fact_makes_it_reachable_from_its_siblings` 覆盖"文档内写入 → 兄弟文档召回不到 → 提升到项目 → 召回得到"这条完整链路。全部通过（二期完成时 Python 侧 411 passed）。

### 三期：召回（should → must）

10. `_domain_predicate` + overfetch + relevance 修正；
11. dsh 侧会话 domain 注入 + 查询推断 + MUST 准入；
12. 三层阶梯 + 短路 + `retrieval_level` + `filtered|empty`；
13. 灰度 `off → should → must`，观察第七节六项指标。

**验收**：目标 1（教学查询不含旅游）与目标 2（教学 scope 内召回编程）各一条端到端用例；`filtered` 比例告警接线。

### 四期：摘要与打磨

14. compact 剥离 domain 前缀；知识体按 domain 分组**只用在 detail 深度**（现有 `_BLOCK_SHARE_*` 之和已故意 oversubscribe，`summary.py:165-169`，再加分组块会更快吃掉"全局规则"的保留份额）；
15. `domain_signal` 审核界面；`domain` 树的可视化管理。

---

## 十一、这个方案解决了什么、没解决什么

**解决（相比"只有 scope"的今天）**

- 同 scope 内的主题干扰：user / global 级的旅游、生活记忆不再进入教学查询的候选池——这是今天最容易发生、也最容易被误认为"召回不准"的一类。
- 跨项目教学积累：`user` scope + `teaching` domain 的偏好，对每一门课都可见。
- 一致性：清库换来 I1 / I2 两条硬约束，`general` 只是兜底而不是默认桶，MUST 过滤因此真的能隔离。

**没解决（要明确写进预期）**

- **历史记忆不会自己变好**——本方案的前提就是不要历史数据。
- **document 级 scope 造成的不可见**：靠 4.1 / 4.2 缓解，但如果用户坚持"每章一个文档、互不可见"，那么任何维度都救不了这个召回——那是用户的组织方式，不是存储的问题。
- **domain 打标错误**：靠第七节的评测集度量、靠 `memory_add` 回执 + `fact_domain_set` 修正。这是概率问题不是机制问题。
- **"召回全部教学知识"不保证**：铁律 3。MUST 只管"不混入"，"找得到"永远是 scope 的职责。
