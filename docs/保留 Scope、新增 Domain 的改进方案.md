# 保留 Scope、新增 Domain 的改进方案

## 一、设计目标

在不破坏现有 scope 机制的前提下，引入 domain 作为独立的主题分类维度，解决三个问题：

1. **主题隔离**：教学、编程、旅游等不同领域的记忆不应互相干扰。
2. **跨领域可用**：在教学项目里写脚本、在编程项目里做教学文档，不应因为默认 domain 而召回为空。
3. **低摩擦**：写入时不需要用户逐条确认，召回时不要求用户显式指定 domain。

核心判断是：**scope 是上下文过滤条件，domain 是主题排序信号和可调过滤条件，两者都不是会话级常量。**

---

## 二、核心原则

1. **正交不替代**：scope 管“在什么上下文”，domain 管“关于什么主题”。
2. **scope 单一归属，domain 可多标签**：一条事实必须属于一个 scope，可以同时属于多个 domain。
3. **scope 自动推断，domain 半自动**：scope 从环境解析；domain 由 LLM 建议、规则兜底、会话默认继承，用户可事后修正。
4. **domain 不是会话常量**：会话默认 domain 只是初始候选和排序偏置，不是 MUST 过滤条件。
5. **过滤优先于排序，但过滤强度可调**：scope 始终是 MUST；domain 分 must / should / off 三级。
6. **结果不足自动降级**：不返回空，逐层放宽。
7. **读取不创建，写入注册制**：召回不创建 scope，也不创建 domain；新 domain 先记信号，后审核。
8. **指纹不含 domain，冲突域含 domain 交集**。

---

## 三、数据模型

### 3.1 新增表

- **domains 表**：存储 domain 的层级结构。字段包括唯一标识、父级引用、canonical 名称、显示名称、描述、归档标记、创建与更新时间。canonical 名称唯一，路径式命名，小写 ASCII，用斜杠分隔。层级遍历只靠父级引用，不解析路径字符串。
- **fact_domains 表**：事实与 domain 的多对多关联。字段包括事实标识、domain 标识、是否主 domain、置信度、创建时间。主 domain 用于排序和冲突判断的优先级。
- **domain_signals 表**：记录写入时建议但未注册的 domain。字段包括建议名称、关联 scope、出现次数、首次与最近出现时间。达到阈值后由用户或管理员审核注册。

### 3.2 保持不变

- scopes 表结构不变。
- facts 表的 scope_id 不变，仍是单一归属。
- facts_fts 和 facts_vec 不变。
- events、profile、task_queue 不变。

### 3.3 不把 domain 塞进 facts 表的原因

- domain 多标签，单一字段无法表达。
- domain 需要独立注册、层级、归档。
- 未来可能需要权重、来源、审核状态，独立表更易扩展。
- 不破坏现有 facts 表的写入路径。

---

## 四、Domain 规范与注册

### 4.1 Canonical 命名

- 小写 ASCII，路径式，斜杠分隔，例如 teaching、teaching/ds、teaching/ds/ch3、programming、life/travel。
- 禁止空格、中文、大小写混用。
- 中文显示名称单独存。
- 路径只用于展示，层级靠父级引用。

### 4.2 初始注册

内置少量根 domain：general、teaching、programming、life。用户可在配置中扩展。

### 4.3 注册制

- 新 domain 不自动创建。
- 写入时如果 LLM 建议了未注册 domain：
  - 落入最近的已注册祖先 domain。
  - 记录到 domain_signals。
  - 达到阈值后由用户或管理员审核注册。
- 召回时只读，不创建。

### 4.4 层级解析

解析 domain 时，先精确匹配 canonical 名称；匹配不到则逐级向上找已注册祖先；写入时可以按需创建父链，召回时只返回最近祖先或空。

---

## 五、写入路径

### 5.1 LLM 抽取

在抽取提示词中，除现有 scope_hint 外，增加 domain_hints 和 primary_domain。要求：

- domain_hints 只能从提供的 canonical 列表中选择，或从当前会话默认 domain 继承。
- 最多三个。
- primary_domain 必须在 domain_hints 中。
- 不确定时留空，由系统回退。

### 5.2 规则兜底

Python 侧规则抽取器维护关键词到 domain 的映射。规则命中则建议 domain；未命中则继承当前会话默认 domain。

### 5.3 会话默认 domain

会话开始时解析默认 domain 集合，来源包括：

- scope 的 canonical 名称映射到 domain。
- 会话显式标签。
- 工作目录、git remote、项目配置。
- 兜底 general。

默认 domain 集合包含祖先链，例如 teaching、teaching/ds、teaching/ds/ch3。

### 5.4 写入校验链

在现有校验链中插入 domain 校验：

1. **合法性**：每个 domain_hint 必须能解析到已注册 domain 或祖先。
2. **数量上限**：最多三个，超过截断。
3. **主 domain 一致性**：primary_domain 必须在集合中，否则取第一个。
4. **回退**：集合为空时使用当前会话默认 domain 集合。
5. **去重**：同一事实的 domain 去重。
6. **写入**：facts 插入后写 fact_domains。

### 5.5 写入事务

在同一事务中插入 facts、fact_domains、facts_fts、facts_vec，保持与现有写入路径一致，不引入新的事务边界。

### 5.6 写入结果渲染

memory_add 返回中增加 domain 信息，显示主 domain、其他 domain、scope，以及未注册 domain 的回退提示。用户看到后可以主动纠正，不弹窗。

### 5.7 半自动打标的含义

半自动不是“一半自动一半弹窗”，而是：

- 系统自动推断、默认回退、静默写入、记录信号、用户可事后修正。
- 只有高风险场景才提示，而且用非阻塞方式：工具结果提示、系统提示词待办、专门的管理命令、宿主通知。
- 绝不默认弹窗打断对话。

### 5.8 打标模式配置

提供配置项：off（不自动打标，全落 general）、auto（默认，静默打标）、confirm_new（未注册 domain 时提示，不弹窗）、confirm_all（每条提示，走汇总队列）。默认 auto。

---

## 六、召回路径

### 6.1 三层 domain 解析

| 层级             | 来源                           | 稳定性       | 用途               |
| ---------------- | ------------------------------ | ------------ | ------------------ |
| 会话默认 domain  | scope 映射、项目配置、显式标签 | 会话内稳定   | 初始候选、排序偏置 |
| 查询推断 domain  | LLM 从 query 推断，规则兜底    | 每次查询变化 | 修正意图           |
| 有效 domain 集合 | 前两者合并加回退               | 查询时确定   | 实际过滤与排序     |

关键点：查询推断 domain 可以覆盖或扩展会话默认 domain，不被锁死。

### 6.2 三级过滤强度

- **must**：候选必须属于有效 domain 集合。适用于意图明确、domain 高置信。
- **should**：不硬过滤，匹配 domain 加权。适用于意图模糊或混合意图。默认强度。
- **off**：不过滤，仅用 scope。适用于查询明显跨领域或结果不足。

### 6.3 查询推断 domain

- **LLM 推断**：在查询预处理中用轻量调用，返回推断 domain、置信度、理由。
- **规则兜底**：关键词映射。命中多个 domain 视为多意图；无命中则退化到会话默认。
- **升级到 must 的条件**：置信度高于阈值、只有一个推断 domain、与会话默认不冲突或明显更强、查询文本无跨领域信号。否则一律 should。

### 6.4 有效 domain 集合的扩展

- **祖先 domain**：查询 programming 时也召回父级通用编程偏好。
- **桥接 domain**：配置跨领域桥接，如 teaching 与 programming、teaching 与 life。命中一个时桥接的另一个以 should 权重加入。
- **多意图拆分**：查询明显包含多个意图时拆成子查询，分别召回再合并，保留各自 domain 标记。
- **主与次 domain**：查询推断高置信的为 primary，会话默认、桥接、祖先为 secondary。排序时 primary 权重更高，但不排除 secondary。

### 6.5 Scope 过滤

scope 始终是 MUST。允许的 scope 集合为当前 scope 加祖先加后代。FTS5 和向量检索都在检索阶段按 scope 过滤，不放到排序阶段。

### 6.6 Domain 过滤

必须在 RRF 融合之前做，避免无关领域挤占名额。must 模式下用 EXISTS 或内存过滤；should 模式下不硬过滤，在融合后加权。

### 6.7 排序公式调整

在 should 模式下加 domain 项，其他项按比例缩放，保持总和为一。domain_match 计算：主 domain 命中为一，secondary 为中等，桥接为较低，未命中为零。must 模式不加 domain 项，因为已硬过滤。off 模式也不加。

### 6.8 分层召回

不要只做一次召回，用逐层放宽：

1. **第一层**：scope 严格加 domain 严格（must）。意图明确时精准命中。
2. **第二层**：scope 严格加 domain 加权（should）。混合意图时同 domain 优先，不排除其他。
3. **第三层**：scope 严格加 domain 关闭。跨领域查询时只在当前 scope 内检索。
4. **第四层**：scope 放宽到祖先加 domain 关闭。允许 user 级通用记忆进入。
5. **第五层**：全局加 domain 加权。跨上下文参考，标记来源。

每层有最小结果数阈值，不足则自动进入下一层，不直接返回空。每层结果记录 retrieval_level，渲染时区分：当前上下文、同主题扩展、跨主题参考、跨上下文参考。

### 6.9 防止空结果的兜底

- 结果不足自动降级。
- 所有层都空时返回明确提示，包含当前 scope、尝试的 domain、是否扩大检索范围的建议，而不是静默空结果。
- 每次降级写 events，记录查询、会话 domain、推断 domain、尝试层级、最终层级、结果数，用于后续调优。

### 6.10 会话默认 domain 的重新定义

会话默认 domain 是初始候选加排序偏置，不是过滤条件。召回时不把它当 MUST。查询推断 domain 优先于会话默认 domain。会话默认 domain 仍用于写入回退、快照分组、召回 secondary。

---

## 七、冲突解决

### 7.1 冲突域

冲突域为 scope 加 subject、predicate、object 加 domain 交集。规则：

- 不同 scope 不冲突。
- 同 scope 但 domain 无交集不冲突。
- 同 scope 且 domain 有交集进入现有冲突策略。
- 无 domain 视为 general，只与其他无 domain 事实比较。

### 7.2 冲突策略不变

多值谓词下不同对象不是冲突；相同对象加相同否定是幂等；单值谓词下较新获胜，除非旧证据明显更强；证据权重公式保持 confidence 加 importance 的加权。

### 7.3 取代与拒绝

取代时新事实写入，旧事实标记 superseded_by，fact_domains 按新事实写入，events 记录 scope 和 domain。拒绝时不写入，events 记录原因和 domain。

### 7.4 跨 domain 的通用事实

例如“用户喜欢先讲概念再举例”，scope 放 user 或教学项目，domain 放 teaching，可选 teaching/ds。冲突时只在 teaching 域内比较，不影响 programming。

---

## 八、指纹与去重

### 8.1 指纹定义

保持基于规范化内容，不包含 domain。domain 是观察属性，不是主张的一部分。包含 domain 会导致同一事实重复存储。

### 8.2 幂等写入

指纹匹配到已有事实时不新增，而是强化复用计数、合并 domain（把新 domain 加入 fact_domains）、更新 last_used_at。如果新 domain 与已有 domain 冲突，进入冲突策略。

### 8.3 去重范围

- 同 scope 内指纹匹配：强化加合并 domain。
- 跨 scope 指纹匹配：不自动合并，视为不同上下文的事实。
- 可选：跨 scope 但 domain 完全相同时提示用户是否合并。

---

## 九、摘要与快照

### 9.1 Compact 渲染

剥离 domain 前缀，避免吃掉字符上限。每条 compact 行只含 scope 短标签、内容、重要性与置信度。domain 默认不进入 compact 行，需要时可用短标签，但默认关闭。

### 9.2 知识体渲染

按 domain 分组，每组内按有效重要性排序，组间按当前 domain 匹配度排序。

### 9.3 会话快照冻结

会话开始时冻结当前 scope 加当前 domain 的快照，包含当前 scope 及祖先的通用偏好、当前 domain 及祖先的知识体、用户画像。会话期间不变化，避免每请求重算。

---

## 十、用户画像

保持按 user scope 存，行数上限和拒绝新增策略不变。进入系统提示词的画像行不加 domain 前缀。可选地给画像行关联 domain 用于渲染分组，但不参与冲突和召回过滤，默认不启用。

---

## 十一、TS 侧变更

- **bridge**：recall 请求构造时注入 scope_ids 和 domain_ids；memory_add 响应解析 domain 信息并渲染；会话初始化时获取当前 domain 集合。
- **llm-extractor**：抽取提示词增加 domain 部分，提供 canonical 列表，校验返回的 domain_hints，不合法则重试一次，仍失败则丢弃 domain 由系统回退。
- **context**：新增会话 domain 解析，从 scope 映射、显式标签、工作目录、git remote 解析，兜底 general，缓存结果会话期间不变化。
- **工具暴露**：memory_recall 增加可选 domain 参数但默认由系统注入；memory_add 增加可选 domain_hints 供用户显式指定；新增 memory_domains 工具列出当前可用 domain。

---

## 十二、迁移策略

不保留现有数据。

---

## 十三、索引与性能

- **索引**：fact_domains 按 domain_id 加 fact_id 和按 fact_id 建索引；domains 按 parent_id 建索引，canonical_name 唯一索引。
- **查询计划**：先用 FTS5 取 top-N（N 为 k 的数倍），再用 scope 和 domain 过滤，避免 EXISTS 子查询拖慢 FTS5。向量侧先 KNN 取 top-N，内存过滤 scope 和 domain，再融合。
- **缓存**：domain 树缓存到内存，写入时失效；当前会话 domain 集合缓存；允许的 scope 和 domain 集合缓存。

---

## 十四、配置项

- **domain_recall**：默认模式（should）、must 置信阈值、每层最小结果数、是否启用查询推断、是否启用桥接、是否启用多意图拆分、是否回退全局、是否记录降级。
- **domain_bridges**：跨领域桥接列表，如 teaching 与 programming、teaching 与 life。
- **domain_tagging_mode**：off、auto、confirm_new、confirm_all，默认 auto。

---

## 十五、风险与对策

| 风险                    | 对策                            |
| ----------------------- | ------------------------------- |
| LLM 漏标或错标 domain   | 后处理校验加回退默认 domain     |
| Domain 数量爆炸         | 注册制加信号表加上限            |
| Domain 命名漂移         | Canonical 规范加只读解析        |
| 多标签冲突域复杂        | 交集判断加主 domain             |
| 旧数据回填不准          | 离线批量加人工审核加灰度        |
| FTS5 查询变慢           | 先 FTS5 top-N 再过滤            |
| 向量过滤遗漏            | 内存后过滤加 must 模式          |
| Compact 快照 token 膨胀 | 剥离 domain 前缀                |
| 跨 scope 通用事实重复   | 放祖先 scope 或 user scope      |
| Domain 重命名或合并     | 重写 fact_domains，不重写 facts |

---

## 十六、实施顺序

1. **数据模型**：迁移建表，插入根 domain，只写 fact_domains，不启用过滤。
2. **写入路径**：LLM 抽取加 domain，规则回退加 domain，校验链加 domain 校验，memory_add 渲染 domain。
3. **召回路径**：recall 接受 domain_ids，FTS5 和向量侧加过滤，后过滤加多级召回。灰度从 should 到 must。
4. **冲突与摘要**：冲突域加 domain 交集，compact 剥离 domain，知识体按 domain 分组。
5. **迁移与回填**：离线回填现有 facts，人工审核 domain_signals，注册新 domain。
6. **观测与调优**：记录召回命中率、domain 分布，调整 domain 列表和映射表，必要时引入 domain 层级。

---

## 十七、总结

这套改进的核心是：

- **scope 不动**：继续负责上下文隔离、自动推断、祖先继承、冲突域基础。
- **domain 独立**：多标签、可层级、注册制、半自动。
- **写入**：LLM 建议加校验加回退默认，静默写入，不弹窗。
- **召回**：scope 始终 MUST，domain 分 must / should / off 三级，默认 should。
- **domain 不是会话常量**：查询推断可以覆盖会话默认，避免跨领域任务失效。
- **分层召回**：严格到加权到关闭到放宽 scope 到全局，结果不足自动降级。
- **冲突**：scope 加 domain 交集。
- **指纹**：不含 domain。
- **迁移**：灰度、回填、审核。

最终效果是：在教学项目里问“写 Python 脚本”，系统能推断出 programming 意图，召回之前积累的编程记忆；在编程项目里做教学文档，也能召回教学知识。同时，纯粹的教学查询仍然优先命中教学记忆，不会被编程和旅游干扰。