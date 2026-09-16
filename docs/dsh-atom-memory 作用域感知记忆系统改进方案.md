# dsh-atom-memory 作用域感知记忆系统改进方案

## 一、背景与问题

### 1.1 现状

当前系统把原子事实作为唯一存储单位，所有事实共享一个全局池。写入时通过 `dedupMaxDistance` 做语义去重，检索时用向量 + FTS5 + RRF + 四因子重排。系统没有项目、客户、文档、阶段等作用域维度。

### 1.2 核心问题

| 问题               | 表现                                                        |
| ------------------ | ----------------------------------------------------------- |
| 跨项目污染         | 项目A的经验被项目B召回并应用                                |
| 跨项目经验无法复用 | 全局规则（如"TypeScript 严格模式要开"）被随机绑定或无法表达 |
| 项目内经验演化丢失 | 同一项目不同阶段的经验互相覆盖                              |
| 去重/冲突误判      | 不同项目的相似经验被合并或判矛盾                            |
| 摘要注入无作用域   | 会话启动时全局摘要混入无关项目内容                          |
| 办公/写作场景失效  | 没有 git 指纹，无法用仓库识别项目                           |

### 1.3 根本原因

系统把"作用域"当作隐含的语义属性，而不是显式的存储维度。事实缺少作用域绑定、条件约束、层级关系和演化表达。

---

## 二、设计原则

1. **作用域是维度，不是外键**：一条事实可以关联多个作用域，作用域有层级。
2. **全局 + 条件优先于项目绑定**：不确定时宁可放全局加条件，也不要绑到具体项目。
3. **向上继承，向下覆盖**：召回时展开祖先作用域，具体作用域优先级更高。
4. **软身份，多信号**：作用域识别用多信号指纹，名称只作别名。
5. **低置信不自动合并**：新建作用域标记低置信，等确认或更多证据。
6. **冲突按作用域处理**：同作用域内判演化，跨作用域只建链接或标参考。
7. **阶段是演化，不是覆盖**：旧阶段事实保留并标注，不直接删除。
8. **解析不确定时降级到 global**：漏召回比污染更容易发现和修复。

---

## 三、数据模型

### 3.1 作用域表

```sql
CREATE TABLE scope (
  id              INTEGER PRIMARY KEY,
  scope_type      TEXT NOT NULL,      -- global/user/org/team/client/project/series/phase/document/thread
  canonical_name  TEXT NOT NULL,
  parent_id       INTEGER REFERENCES scope(id),
  path            TEXT NOT NULL,      -- 物化路径，如 /global/org:a/client:b/project:c
  status          TEXT NOT NULL DEFAULT 'active',  -- active/merged/archived
  merged_into     INTEGER REFERENCES scope(id),
  confidence      REAL NOT NULL DEFAULT 0.5,
  created_at      INTEGER NOT NULL,
  last_seen_at    INTEGER NOT NULL,
  CHECK (scope_type IN ('global','user','org','team','client','project','series','phase','document','thread'))
);

CREATE INDEX idx_scope_parent ON scope(parent_id);
CREATE INDEX idx_scope_path   ON scope(path);
CREATE INDEX idx_scope_type   ON scope(scope_type, status);
```

`global` 是根作用域，`path = '/global'`，全局唯一。

### 3.2 作用域别名表

```sql
CREATE TABLE scope_alias (
  id          INTEGER PRIMARY KEY,
  scope_id    INTEGER NOT NULL REFERENCES scope(id),
  alias       TEXT NOT NULL,
  alias_type  TEXT NOT NULL,   -- name/path/remote/doc_id/folder_id/email_thread/project_code
  confidence  REAL NOT NULL DEFAULT 0.5,
  first_seen  INTEGER NOT NULL,
  last_seen   INTEGER NOT NULL
);

CREATE UNIQUE INDEX idx_alias_unique ON scope_alias(alias_type, alias);
CREATE INDEX idx_alias_scope ON scope_alias(scope_id);
```

### 3.3 作用域信号表

```sql
CREATE TABLE scope_signal (
  id               INTEGER PRIMARY KEY,
  scope_id         INTEGER NOT NULL REFERENCES scope(id),
  signal_type      TEXT NOT NULL,   -- git_root/remote/package/doc_id/folder_id/participants/content_anchor
  signal_value     TEXT NOT NULL,
  normalized_value TEXT NOT NULL,
  confidence       REAL NOT NULL DEFAULT 0.5,
  first_seen       INTEGER NOT NULL,
  last_seen        INTEGER NOT NULL
);

CREATE UNIQUE INDEX idx_signal_unique ON scope_signal(signal_type, normalized_value);
CREATE INDEX idx_signal_scope ON scope_signal(scope_id);
```

### 3.4 事实与作用域多对多

```sql
CREATE TABLE fact_scope (
  fact_id   INTEGER NOT NULL,
  scope_id  INTEGER NOT NULL,
  priority  INTEGER NOT NULL DEFAULT 0,  -- 具体作用域优先级更高
  PRIMARY KEY (fact_id, scope_id)
);

CREATE INDEX idx_fact_scope_scope ON fact_scope(scope_id);
```

### 3.5 事实条件表

```sql
CREATE TABLE fact_condition (
  fact_id  INTEGER NOT NULL,
  key      TEXT NOT NULL,   -- language/doc_type/industry/audience/stage/tool/vcs/...
  value    TEXT NOT NULL,
  PRIMARY KEY (fact_id, key, value)
);

CREATE INDEX idx_condition_kv ON fact_condition(key, value);
```

条件用于表达"跨项目但有条件"的经验，例如 `doc_type=proposal`、`language=typescript`。

### 3.6 事实来源链接表

```sql
CREATE TABLE fact_origin (
  derived_fact_id INTEGER NOT NULL,  -- 抽象/提升后的事实
  source_fact_id  INTEGER NOT NULL,  -- 原始项目事实
  relation        TEXT NOT NULL,     -- abstraction/generalization/exception
  PRIMARY KEY (derived_fact_id, source_fact_id)
);
```

用于跨项目经验提升：多个项目重复出现的模式抽象为全局规则，保留原始事实作为证据。

### 3.7 与现有表的关系

保留现有 `fact` 表，新增上述表。`fact` 不再需要 `project_id` 字段，作用域关系全部通过 `fact_scope` 表达。

---

## 四、作用域解析

### 4.1 上下文信号采集

会话开始时从环境中采集信号，按可靠性排序：

| 信号类型    | 编程场景                      | 办公/写作场景                     | 可靠性 |
| ----------- | ----------------------------- | --------------------------------- | ------ |
| 显式元数据  | 会话配置                      | 用户标签、任务工具项目字段        | 极高   |
| 持久 ID     | git 根提交哈希                | 云文档 ID、文件夹 ID、邮件线程 ID | 极高   |
| 远程标识    | git remote 规范化 URL         | 共享链接、组织域名                | 高     |
| 包/项目标识 | package.json / pyproject.toml | 文档标题、项目代号                | 中     |
| 路径        | 工作目录                      | 文件夹路径                        | 中     |
| 参与者      | 团队成员                      | 客户联系人、审批人                | 中     |
| 内容锚点    | 技术栈、模块名                | 客户名、行业、主题                | 低     |
| 显示名称    | 仓库名                        | 文档名                            | 极低   |

### 4.2 解析流程

```
输入：会话上下文 + 记忆文本 + 显式元数据
  │
  ▼
1. 信号提取
   从上下文、元数据、文本中抽取候选信号
  │
  ▼
2. 候选作用域生成
   用 scope_alias / scope_signal 查询匹配
   按信号可靠性加权
  │
  ▼
3. 置信度判断
   高置信（持久 ID 精确匹配）→ 直接绑定
   中置信（规范化后匹配多个）→ 选最可能的，标记待确认
   低置信（仅名称相似）→ 不自动新建，降级到 global + condition
  │
  ▼
4. 作用域选择
   已有合适 scope → 绑定
   无合适 scope 但信号强 → 新建 scope，标记 confidence
   无合适 scope 且信号弱 → 绑定 global，加 condition，标记 unresolved
  │
  ▼
5. 层级判断
   通用规则 → global
   组织规范 → org/team
   客户偏好 → client
   项目特定 → project/series
   阶段特定 → phase
   文档特定 → document/thread
```

### 4.3 置信度阈值

| 置信度    | 处理                                                         |
| --------- | ------------------------------------------------------------ |
| ≥ 0.9     | 自动绑定，不询问                                             |
| 0.6 ~ 0.9 | 自动绑定，标记 `pending_confirmation`，下次会话可确认        |
| 0.3 ~ 0.6 | 绑定 global + condition，记录候选 scope 到 `unresolved` 队列 |
| < 0.3     | 绑定 global，无条件，等待重复出现后再提升                    |

### 4.4 新建作用域的条件

只有满足以下之一才新建：

- 存在高置信信号，且与现有作用域无匹配
- 用户显式创建
- 同一候选在 `unresolved` 队列中出现 N 次（默认 3 次）且信号一致

否则一律降级到 global 或父级作用域。

### 4.5 别名与合并

- 新信号与已有作用域匹配时，写入 `scope_alias` 或 `scope_signal`，不新建。
- 发现两个作用域实为同一项目时，人工或高置信自动合并：旧 scope 的 `status='merged'`，`merged_into` 指向新 scope，事实关系迁移。
- 合并保留历史，不删除旧 scope。
- 拆分场景：新建子 scope，迁移部分事实，旧 scope 保留。

---

## 五、存储流程

### 5.1 写入路径

```
新记忆候选
  │
  ▼
1. 抽取：LLM 或规则抽取事实文本 + 作用域提示 + 条件
  │
  ▼
2. 作用域解析（见第四章）
  │
  ▼
3. 去重检查
   仅在同一作用域集合内检查语义距离
   跨作用域相似事实不自动合并
  │
  ▼
4. 冲突检查
   同作用域内：判 superseded / retracted
   跨作用域：只建 fact_origin 链接，不判矛盾
  │
  ▼
5. 写入
   fact + fact_scope + fact_condition
  │
  ▼
6. 更新索引
   向量索引、FTS5 索引
  │
  ▼
7. 更新 scope 的 last_seen_at
```

### 5.2 抽取时的作用域提示

在抽取提示词中要求 LLM 输出：

```json
{
  "fact": "TypeScript 严格模式要开",
  "scope_hint": "global",
  "conditions": [{"key": "language", "value": "typescript"}],
  "confidence": 0.9
}
```

LLM 不直接决定 scope_id，只提供提示。最终作用域由解析流程决定。

### 5.3 去重规则

| 场景                       | 处理                                                  |
| -------------------------- | ----------------------------------------------------- |
| 同作用域 + 语义距离 < 阈值 | 折叠合并，保留更高 importance                         |
| 同作用域 + 语义距离 ≥ 阈值 | 作为新事实                                            |
| 跨作用域 + 语义距离 < 阈值 | 不合并，建 fact_origin 链接，标 `cross_scope_similar` |
| 跨作用域 + 条件相同        | 不合并，但可提升为全局候选                            |

### 5.4 冲突规则

| 场景                    | 处理                                                         |
| ----------------------- | ------------------------------------------------------------ |
| 同作用域 + 直接矛盾     | 按证据判 superseded / retracted                              |
| 同作用域 + 阶段不同     | 保留双方，标注 phase，建立演化链接                           |
| 跨作用域 + 矛盾         | 不判矛盾，标 `scope_priority`，让 LLM 看到"全局规则 + 项目例外" |
| 跨作用域 + 具体覆盖一般 | 保留双方，具体作用域优先级更高                               |

### 5.5 跨项目经验提升

当同一模式在多个作用域重复出现：

1. 保留各作用域下的原始事实。
2. 新建一条 `global` 或父级作用域事实。
3. 用 `fact_origin` 记录来源。
4. 原始事实继续存在，作为证据。
5. 召回时优先返回抽象规则，项目细节按需展开。

提升触发条件：

- 同一语义模式在 ≥ 3 个不同作用域出现
- 各作用域下事实均 active
- 无相互矛盾

---

## 六、召回流程

### 6.1 上下文解析

会话/查询开始时解析当前上下文：

- 当前 scope 路径（如 `/global/org:a/client:b/project:c/phase:draft`）
- 当前条件（doc_type、language、industry、audience、stage、tool）
- 当前查询文本

### 6.2 作用域展开

```
当前 scope
  + 所有祖先 scope（沿 parent_id 向上）
  + global
  + 条件匹配的其他 scope（降权）
```

例如当前是 `project:c`，展开为：

```
/global
/global/org:a
/global/org:a/client:b
/global/org:a/client:b/project:c
```

### 6.3 候选召回

```sql
-- 1. 作用域路径上的事实
SELECT f.* FROM fact f
JOIN fact_scope fs ON f.id = fs.fact_id
WHERE fs.scope_id IN (:scope_path_ids)
  AND f.status = 'active'

-- 2. global 事实
-- 已包含在 scope_path_ids 中

-- 3. 条件匹配的事实
SELECT f.* FROM fact f
JOIN fact_condition fc ON f.id = fc.fact_id
WHERE fc.key = :current_key AND fc.value = :current_value
  AND f.status = 'active'

-- 4. 其他作用域语义相似且条件匹配（降权，作为参考）
```

### 6.4 排序公式

```
score = 0.30 · semantic_rrf
      + 0.20 · scope_distance_weight
      + 0.15 · condition_match
      + 0.10 · importance
      + 0.10 · recency
      + 0.10 · trust
      + 0.05 · phase_match
```

`scope_distance_weight`：

| 关系                | 权重 |
| ------------------- | ---- |
| 当前 scope 精确匹配 | 1.0  |
| 父级 scope          | 0.8  |
| 祖父级              | 0.6  |
| global              | 0.5  |
| 同级其他 scope      | 0.3  |
| 其他项目条件匹配    | 0.15 |

`condition_match`：

- 全部条件匹配：1.0
- 部分匹配：按比例
- 无条件：0.5（中性）

### 6.5 作用域优先级

具体作用域覆盖一般作用域，但保留例外：

```
优先级：session > task > document > phase > project > client > org > user > global
```

召回时同时返回具体规则和一般规则，标注优先级，让 LLM 看到完整上下文，而不是把一般规则标为矛盾。

### 6.6 阶段处理

- 默认召回所有阶段的 active 事实。
- 当前阶段权重最高。
- 旧阶段事实标注 `phase=...`，可见但不主导。
- 阶段冲突不判矛盾，建立演化链接。
- 阶段切换可由用户显式标记，或系统检测（依赖清单大改、架构目录变化、文档状态变更）。

### 6.7 摘要注入

会话启动时的冻结摘要分块生成：

```
===== BEGIN MEMORY-DATA =====
| [当前项目] ...
| [当前客户] ...
| [全局规则] ...
| [条件规则: doc_type=proposal] ...
| [其他项目参考] ...
===== END MEMORY-DATA =====
```

每块独立受 token 预算控制，当前项目优先。全局规则始终包含，但可压缩。其他项目参考默认不注入，仅在检索时按需返回。

---

## 七、冲突与演化

### 7.1 同作用域内

| 情况     | 处理                                       |
| -------- | ------------------------------------------ |
| 直接矛盾 | 按证据判 superseded / retracted            |
| 阶段演化 | 保留双方，标 phase，建立 `evolves_to` 链接 |
| 条件不同 | 保留双方，条件区分                         |
| 重复     | 折叠合并                                   |

### 7.2 跨作用域

| 情况                 | 处理                                                      |
| -------------------- | --------------------------------------------------------- |
| 具体覆盖一般         | 保留双方，具体作用域优先级更高                            |
| 相似但不矛盾         | 建 `cross_scope_similar` 链接                             |
| 矛盾                 | 不自动判矛盾，标 `scope_conflict`，人工确认或按优先级处理 |
| 同一模式多作用域出现 | 提升为全局候选                                            |

### 7.3 演化表达

```sql
CREATE TABLE fact_evolution (
  from_fact_id  INTEGER NOT NULL,
  to_fact_id    INTEGER NOT NULL,
  relation      TEXT NOT NULL,  -- evolves_to/supersedes/exception/abstraction
  scope_id      INTEGER,
  reason        TEXT,
  created_at    INTEGER NOT NULL,
  PRIMARY KEY (from_fact_id, to_fact_id, relation)
);
```

---

## 八、办公/写作场景适配

### 8.1 作用域类型

办公/写作场景扩展 `scope_type`：

- `client`：客户/账户
- `series`：系列（如专栏、产品线）
- `document`：文档
- `thread`：邮件线程
- `phase`：阶段（选题/调研/大纲/初稿/评审/定稿/发布）

### 8.2 条件维度

- `doc_type`：proposal/report/email/note/spec
- `audience`：executive/peer/client/public
- `industry`：finance/health/tech/...
- `language`：zh/en/...
- `tool`：notion/word/email/...
- `stage`：draft/review/final

### 8.3 识别信号

- 云文档持久 ID
- 文件夹 ID
- 邮件线程 ID
- 日历事件 ID
- 任务工具项目字段
- 参与者集合
- 文档标题、项目代号
- 用户显式标签

### 8.4 示例

| 事实                 | 作用域         | 条件                                  |
| -------------------- | -------------- | ------------------------------------- |
| 对外提案先写执行摘要 | global         | doc_type=proposal, audience=executive |
| 客户A要求正式语气    | client:A       | —                                     |
| 金融报告需加风险提示 | global         | industry=finance                      |
| 2024Q3 报告含新 KPI  | project:2024Q3 | —                                     |
| 定稿阶段逐条核对数字 | global         | stage=final                           |

---

## 九、与 dsh-atom-memory 的集成

### 9.1 需要修改的模块

| 模块                             | 改动                                                         |
| -------------------------------- | ------------------------------------------------------------ |
| `atom_memory/schema.py`          | 新增 scope/alias/signal/fact_scope/fact_condition/fact_origin/fact_evolution 表 |
| `atom_memory/scope.py`（新增）   | 作用域解析、匹配、新建、合并                                 |
| `atom_memory/context.py`（新增） | 会话上下文信号采集                                           |
| `atom_memory/worker.py`          | 写入流程接入作用域解析                                       |
| `atom_memory/retriever.py`       | 召回流程接入作用域展开和加权                                 |
| `atom_memory/conflict.py`        | 冲突处理按作用域分层                                         |
| `atom_memory/rpc.py`             | 新增 scope 相关 RPC 方法                                     |
| dsh 侧插件                       | 会话启动采集信号，调用 scope 解析                            |

### 9.2 新增 RPC 方法

```
scope_resolve(context)        -> scope_id, confidence, candidates
scope_create(name, type, parent_id, signals) -> scope_id
scope_list(parent_id?)        -> [scope]
scope_merge(from_id, to_id)   -> ok
scope_split(from_id, name, fact_ids) -> new_scope_id
scope_confirm(scope_id)       -> ok
scope_alias_add(scope_id, alias, type) -> ok
fact_scope_bind(fact_id, scope_ids) -> ok
fact_condition_set(fact_id, conditions) -> ok
```

### 9.3 召回接口扩展

```
memory_search(query, scope_context, conditions, limit)
  -> facts with scope annotations
```

`scope_context` 包含当前 scope 路径和条件。

### 9.4 冻结摘要扩展

`memory_summary` 接口增加 `scope_context` 参数，返回分块摘要。

---

## 十、实施路径

### 阶段一：基础作用域

1. 新增 scope / scope_alias / scope_signal / fact_scope 表。
2. 实现 scope_resolve 基础版：名称、路径、持久 ID 匹配。
3. 写入流程接入作用域解析，低置信降级 global。
4. 召回流程展开祖先 + global。
5. 保留现有全局行为作为 fallback。

### 阶段二：条件与多对多

1. 新增 fact_condition 表。
2. 抽取提示词加入条件和作用域提示。
3. 召回加入条件匹配和 scope_distance 加权。
4. 去重和冲突按作用域分层。

### 阶段三：阶段与演化

1. 新增 fact_evolution 表。
2. phase 作为 scope 子类型。
3. 阶段检测和演化链接。
4. 摘要分块注入。

### 阶段四：提升与合并

1. 新增 fact_origin 表。
2. 跨作用域模式提升为全局规则。
3. scope 合并/拆分工具。
4. 低置信 scope 的确认队列。

### 阶段五：办公/写作适配

1. 扩展 scope_type 和 condition key。
2. 云文档/邮件/任务工具信号采集。
3. 办公场景的默认解析策略。
