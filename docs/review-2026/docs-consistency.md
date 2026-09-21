# 文档一致性审查（task-4）

- 审查对象：`README.md`、`README.zh.md`、`README.i18n.yaml`、`docs/*.md`、`docs/diagrams/README.md`、`dsh/README.md`、`dsh/CHANGELOG.md`
- 审查基线：HEAD `bbb67249c0d30d629276cc1b19fb84e502eeeded`
- 解释器：`C:/Program Files/Python314/python.exe`（editable 安装指向本仓库）
- 方法：文档逐条断言 → 代码 `file:line` 双侧取证；数值逐个与常量实测；文档代码示例原样执行；探针置于 `$env:TEMP\dsm-probe\`，未进仓库
- 本报告不修改任何源码或文档原文

---

## 结论摘要

**不通过（文档侧）。** 共 5 项阻塞、9 项建议、5 项风格。

两条主线问题：

1. **README.md 与 README.zh.md 已发生实质性的单边更新**，且中英两侧在同一个位置给出了**互相矛盾的结论**（B2/D1：英文说 `maxFactTokens` 已实现，中文说"目前未实现"；实测已实现）。两侧**结构完全对等**（134 个结构项、37 个标题逐一对应），所以脚本化的结构比对查不出来——必须逐段语义核对，这正是本轮的重点产出。
2. **`memory_domains` 工具在两侧 README 与 `dsh/README.md` 中完全缺载**，而三处文档都写明"十一个工具"，代码注册了**十二个**（B1）。

另外，`docs/reinforcement.md` 的核心数值表（A(n) 曲线）**整表偏离实测约 8%**，属于"文档声称 vs 实测不符"这一类。

---

## 阻塞项

### B1. 工具数量：文档称 11 个，代码注册 12 个，`memory_domains` 全文缺载

- 文档侧：
  - `README.md:346` — "Eleven tool schemas: `memory_add`, …, `memory_scope`, `memory_overview`."
  - `README.zh.md:346` — "十一个工具 schema：…"
  - `README.md:67` / `README.zh.md:67` — 模型可见工具表格同样只列 11 个，**无 `memory_domains`**
  - `dsh/README.md:118-130` — 「工具（模型可见面）」表格同样无 `memory_domains`
- 代码侧：`dsh/src/tools.ts:794,862,889,968,1001,1055,1128,1157,1182,1203,1233,1348` 共 12 个 `ctx.tools.register(defineTool({ name: 'memory_*' }))`；第 12 个即 `dsh/src/tools.ts:1348` 的 `memory_domains`
- 构建产物已同步：`dsh/lib/index.mjs` 含 `memory_domains`（16 处），`git status dsh/lib` 干净
- 影响：模型可见的工具面比文档描述多一个；用户/维护者按文档核对工具集时会漏掉整个主题（domain）维度入口，而它正是 `012` 迁移引入的第二个正交维度
- 建议修复：**改文档**。两侧 README 的表格与"Eleven"改为十二个并补 `memory_domains` 行；`dsh/README.md` 工具表补该行（含 `action` 取值面）

### B2. README.zh.md 与 README.md 在 `maxFactTokens` 上结论相反，中文侧为错误陈述

- `README.zh.md:381` — "**一条长事实可能超出召回预算。** … 要做硬上限就得在渲染侧截断正文；**目前未实现**。"
- `README.md:381` — "**A shortened recall result must be followed up.** `maxFactTokens` bounds one fact … The body is shortened at the ceiling and the fact is flagged …"
- 代码侧（英文侧正确）：`atom_memory/api.py:458` `ceiling = int(self.config.max_fact_tokens or 0)`；`:474-479` `if ceiling > 0 and t > ceiling: body = truncate_to_tokens(...); truncated = True`；`:488` 返回 `"truncated": truncated`；`:428-429` 文档串亦写明。配置默认 `max_fact_tokens = 600`（`atom_memory/config.py`；实测 `MemConfig` 默认值 600）
- 影响：中文读者会认为该保护不存在，从而按"可能超预算 60×"设计调用方；英文读者得到相反（且正确）的结论。同一仓库两侧文档给出相反事实，是最伤用户失败模式
- 建议修复：**改文档**。`README.zh.md:381` 按英文侧重写（上限已实现、正文按上限截断并打 `truncated` 标记、全文由 `memory_get` 取回）

### B3. `docs/reinforcement.md` 的 A(n) 数值表整表偏离实测约 8%

- 文档侧：`docs/reinforcement.md:36-39`
  - 表头 `| n | 0 | 1 | 2 | 3 | 4 | 5 | 8 | ∞ |`
  - `| A(n) | .000 | .111 | .197 | .264 | .316 | .357 | .432 | .500 |`
  - `| marginal | — | +.111 | +.086 | +.067 | +.052 | +.041 | +.019 | → 0 |`
- 代码侧：`atom_memory/reinforce.py:130` `A_MAX = 0.5`、`:135` `N_HALF = 3.0`、`:136` `LAMBDA = math.log(2.0) / N_HALF`、`:287` `value = -shape.a_max * math.expm1(-shape.lambda_n * n)`
- 实测（`reinforce_bonus(n)` 直接调用）：

  | n | 文档 A(n) | 实测 A(n) | 差值 | 实测 marginal |
  |---|---|---|---|---|
  | 0 | .000 | 0.000 | — | — |
  | 1 | .111 | **0.103** | +0.008 | +0.103 |
  | 2 | .197 | **0.185** | +0.012 | +0.082 |
  | 3 | .264 | **0.250** | +0.014 | +0.065 |
  | 4 | .316 | **0.302** | +0.014 | +0.052 |
  | 5 | .357 | **0.343** | +0.014 | +0.041 |
  | 8 | .432 | **0.421** | +0.011 | — |
  | ∞ | .500 | 0.500 | — | → 0 |

- 根因定位：文档数值反解出的隐含 `N_HALF ≈ 2.761`（`λ ≈ 0.2510`），而非代码的 `3.0`（`λ = ln2/3 ≈ 0.23105`）。实测 `N_HALF=2.77` 可复现该整表，说明表格是按一个更早/手算的 `N_HALF` 生成的
- 未失真的部分：`A_MAX / N_HALF / HALF_LIFE_DAYS / COOLDOWN_SEC` 四个参数表（`docs/reinforcement.md:41-46`）与 `KIND_GAINS` 增益表（`:81-86`：1.0/0.8/0.6/0.0）经核对**与代码一致**；`docs/reinforcement.md:143-149` 的 recency 表（半衰期 30 天）经复算**逐格一致**
- 影响：读者按文档数字预估复用强化强度会系统性偏高；`n=3` 处偏差最大（0.264 vs 0.250），恰是"银行半额"的语义锚点
- 建议修复：**改文档**。按 `LAMBDA = ln2/3` 重算两行；建议同时给出生成该表的命令，防止再次手算漂移

### B4. `docs/python-library.md` 的 `stats()` 返回结构缺 2 个字段

- 文档侧：`docs/python-library.md:94` — "| `stats` | `stats(user_id) -> dict` | Counters: `facts`, `pending`. |"
- 代码侧：`atom_memory/api.py:1952` `def stats(self, user_id: str) -> dict:`；`:1982` `"pending"`、`:1983` `"archived"`、`:1984` `"recent": self.recent_outcomes(user_id, limit=5)`
- 实测（真实调用）：`sorted(mem.stats("u").keys()) == ['archived', 'facts', 'pending', 'recent']`
- 影响：契约文档不完整；`recent` 是写回执（含 `outcome.written/superseded/rejected/truncated`）的唯一同步读取口，漏载会让集成方以为只能拿到计数
- 建议修复：**改文档**。补 `archived` 与 `recent`，说明 `recent` 为最近 5 条写结果

### B5. `README.i18n.yaml` 记录的两侧 blob 哈希与 HEAD 全部不符（配对校验记录失效）

- 文档侧：`README.i18n.yaml:14-15` — `README.md: d34cbd9ea802457151c558f0bd531caa989f163e`、`README.zh.md: d2e57c02e39986d58bf533eb6983020a8e2f4c46`
- 实测：`git hash-object README.md` → `44cbaae6e15733910415a339be90266ffe84641d`；`git hash-object README.zh.md` → `ed5900ebd0c8ee7256e22d0e7d5fb4b4de782821`。**两侧均不匹配**
- 该文件自述为"最后一次确认一致状态的 blob 哈希"，其失效意味着**配对一致性已无机器可校验的记录**
- 与 B2 互证：该记录失效期间，两侧确实分叉了（B2 即实证）
- 附带发现：`README.i18n.yaml:4-5` 提到命令 `pnpm run verify-translation-pairing --write README.md`，并在括号中承认本仓库没有该脚本（`package.json` 中确认 `verify-translation` 无匹配）。自承缺口的说明是对的，但校验能力实际为零
- 建议修复：**改文档**。重录两个哈希；且在 B1/B2 修复后必须再录一次（否则新分叉继续无记录）

---

## 建议项

### S1. `dsh/README.md` 的工具表被空行截断为两张表，且 12 个工具只列了 8 个

- `dsh/README.md:126` 是一个空行，使 `:120-125` 与 `:127-130` 成为两张独立表格。渲染后 `memory_recall`/`memory_forget` 与其余工具分表显示，且第二张表无表头
- 同表缺载：`memory_replace`、`memory_get`、`memory_snapshot`、`memory_domains`（代码侧见 B1 行号）
- 影响：渲染破版 + 工具面缺载
- 建议修复：**改文档**。删除 `:126` 空行、补齐 4 行

### S2. `dsh/README.md:59-60` 列举实时生效字段只有 6 个，代码有 7 个

- 文档侧：`dsh/README.md:59-60` — "`enabled`/`llmExtractionEnabled`/`contextInjectionEnabled`/`captureEnabled`/`extractionModel`/`injectedSummaryTokens` 通过 `installSection` 注册为 `atom-memory` 设置命名空间"（6 个，**无 `overviewEnabled`**）
- 代码侧：`dsh/src/index.ts:549-563` `LiveSettingsSchema` 恰好 7 个字段，`:554` 为 `overviewEnabled: z.boolean().default(true)`；`dsh/src/runtime.ts:59` 亦有该字段
- 交叉证据：两侧 README `:75` 的"七个字段"是**正确**的（英文 "the seven fields it owns"、中文"它拥有的七个字段"），只有 `dsh/README.md` 这份列举漏了
- 建议修复：**改文档**。补 `overviewEnabled`

### S3. `docs/python-library.md:110` 的作用域层级不完整（6/10）

- 文档侧：`docs/python-library.md:110` — "A fact belongs to a node in a hierarchy (`org / client / project / phase / document / thread`)"
- 代码侧：`atom_memory/context.py:60-71` `SCOPE_TYPES` 共 10 级：`global, user, org, team, client, project, series, phase, document, thread`
- 同仓库正确表述：`docs/scopes.md:44` 完整列出 10 级；`docs/dsh-atom-memory 作用域感知记忆系统改进方案.md:46,55` 亦完整
- 影响：库契约文档漏掉 `global`/`user`/`team`/`series` 四级；`team` 由 `participants`（0.45）与 `explicit_team` 供给、`series` 由 `explicit_series` 供给（`docs/scopes.md:72,78`），漏载会让集成方以为这些信号无处可去
- 建议修复：**改文档**。补齐 10 级并与 `docs/scopes.md:44` 的写法对齐

### S4. `docs/python-library.md` 的 API 签名漏载新增参数（4 处）

| 方法 | 文档（`docs/python-library.md:88-95`） | 代码实测 |
|---|---|---|
| `forget` | `(user_id, fact_id=None, session_id=None)` | `(user_id, fact_id=None, session_id=None, purge=False, wait_ms=None)` — `purge`/`wait_ms` 未载 |
| `summary` | `(user_id, max_tokens=1500, detail=True, scope_context=None)` | 另有 `overview=None, use_overview=False` 未载 |
| `reinforce` | `(user, fact_id, kind, session_id)` — 参数名 `user`，且看起来必填 | `(user_id, fact_id, kind='user_confirmed', session_id='s_ui')` — **参数名是 `user_id` 不是 `user`**；后两者有默认值 |
| `list_facts` | `(user_id, limit=50, offset=0, include_retracted=False)` | 参数名与默认值一致，但**顺序不同**：`(user_id, include_retracted=False, limit=50, offset=0)` |

- 代码侧：`atom_memory/api.py:1560-1566`（`reinforce`，参数名 `user_id`）、`atom_memory/api.py`（`forget`/`summary` 签名经 `inspect.signature` 实测）
- 影响：`reinforce` 参数名不符会让**按关键字调用**的集成方直接 `TypeError`（`docs/python-library.md:116-117` 还写了 `AtomMem.reinforce(user, fact_id, kind, session_id)`，同理）；`list_facts` 位置传参同样会错位
- 建议修复：**改文档**。`user` → `user_id`；补齐 `purge`/`wait_ms`/`overview`/`use_overview`；`list_facts` 按代码顺序书写

### S5. `docs/diagrams/README.md:64` 的引用条数把两张图写成"各 24 条"，实为 18 与 24

- 文档侧：`docs/diagrams/README.md:64` — "两张 architecture 图另带**仓库源码证据**（各 24 条引用），逐条核对了文件存在性与行号。"
- 实测（解析 `docs/diagrams/src/*.architecture.json`，逐组件计 `sources`）：
  - `system-architecture.architecture.json`：18 个组件 × 1 引用 = **18**
  - `class-structure.architecture.json`：24 个组件 × 1 引用 = **24**
  - 合计 42 条；**42 条引用的文件存在性与行号范围全部校验通过，0 问题**
- 影响：文档数字与产物不符（证据本身是好的，只是计数写错）
- 建议修复：**改文档**。改为"共 42 条引用（`system-architecture` 18 条、`class-structure` 24 条）"

### S6. `docs/python-library.md:299-302` 关于 `candidate_retention_days` 的"已接线"表述与实际接线的窗口方向不一致

- 文档侧：`docs/python-library.md:299-302` — "`candidate_retention_days` was declared but unused before the maintenance pass existed; **it now bounds `fact_candidates` pruning** alongside the other two retention windows."
- 代码侧：`MemConfig` 实测默认值为 `candidate_retention_days = 7`、`task_retention_days = 30`、`event_retention_days = 180`
- **文档 `docs/python-library.md:257` 的 `MemConfig` 清单里写的是 `task_retention_days: int = 14`，代码是 30**
- 影响：配置项默认值文档与代码不符（14 vs 30）；`task_retention_days` 控制 `task_queue` 任务保留窗口，按文档设 14 天会比实际默认更激进地清理
- 建议修复：**改文档**。`docs/python-library.md:257` 改为 `30`

### S7. `docs/python-library.md:266-267` 的 `max_field_chars` / `archive_protect_days` 默认值与代码不符

- 文档侧：`docs/python-library.md:266` `max_field_chars: int = 500`；`:260` `archive_protect_days: int = 30`
- 代码实测：`max_field_chars = 2000`；`archive_protect_days = 14`
- 影响：两个值都直接改变写路径行为——`max_field_chars` 决定 SPO 字段截断点（文档说 500 会让集成方以为 600 字符的取值会被截断，实际不会）；`archive_protect_days` 决定容量控制保护窗口（文档 30 天 vs 实际 14 天，差一倍）
- 建议修复：**改文档**。按实测改为 2000 / 14

### S8. `docs/python-library.md:285` 的 `max_vector_distance` 默认值写成 `None`，dsh 侧实际发 0.70

- 文档侧：`docs/python-library.md:285` — `max_vector_distance: Optional[float] = None`
- 代码侧：库侧 **确为 `None`**（`MemConfig` 实测），但 dsh 侧 `dsh/src/config.ts:236` `maxVectorDistance: z.number().default(0.70)`，即**部署后实际生效值是 0.70**
- 两侧 README 表格都写 `0.70`（`README.md:103` / `README.zh.md:103`），与 dsh 侧一致
- 影响：单独读库文档会以为语义召回默认无距离门限，实际经 dsh 部署后默认有 0.70 门限。属"库默认 vs 部署默认"两个层次的差异，文档未点明
- 建议修复：**改文档**。在 `docs/python-library.md:285` 旁注明"库默认 `None`（无门限）；dsh 插件部署默认发 `0.70`"

### S9. `dsh/README.md:36-37` 把 `enabled` 之外若干字段的作用域描述与 `config.ts` 注释不一致（术语层面）

- 文档侧：`dsh/README.md:36` — "`enabled` … 关闭则停用捕获/上下文注入/记忆工具（运行时热切换）"；`:53` 说明 `scopeEnabled` 关闭后"（连 `scope_context` 键都不发）"
- 代码侧：`dsh/src/config.ts:19` 注释 "when false the plugin is inert (no capture/context/tools)"；`dsh/src/config.ts:188-198` 关于 `scopeEnabled`/`scope*` 为 composition-only、无运行时对应物的说明
- 结论：**语义一致**，此条仅为措辞不一致（`config.ts` 用 "inert"，文档用逐项列举）。列出以防回归，本身无需修复
- 建议修复：无需修复

---

## 风格项

### T1. `dsh/README.md:127-130` 表格缺表头

第二张表（因 S1 的空行而产生）没有 `| 工具 | 说明 |` 表头与分隔行，Markdown 渲染器会把它当成普通文本或错位表。与 S1 同一处修复。

### T2. 两侧 README 代码块语言标注不一致

- `README.md:215` / `README.zh.md:215` 的意识段原文用 ```markdown，正确
- `README.md:281,298` / `README.zh.md:281,298` 的渲染示例用 ```markdown，正确
- `README.md:146-154` / `README.zh.md:146-153` 的进程隔离示意图为无标注围栏（纯文本），可接受但不统一
- 建议：统一为 ```text 或保持现状，非阻塞

### T3. `README.md:18-25` 目录未含 `Known Limitations` 之外的 `Dev Note` 锚点一致性

两侧 TOC 都是 6 项且锚点全部可解析（`#use-this-package` / `#understand-the-implementation` / `#further-exploration` / `#model-experience` / `#known-limitations-and-deferred-work` / `#dev-note`），与文中 `<a id="...">` 逐一对应。**已核对一致，无需修复**；记录于此作为防回归基线。

### T4. 中文侧存在未翻译的英文括号补注

`README.zh.md:68` 「一段常驻的持久记忆意识段（点名工具并指向各自的定义）」——这是中文侧**独有**的旧文本（见 B2 同源问题 D1）。英文侧 `README.md:68` 已精简为 "(always registered)"。属单边更新残留，与 D1 合并修复。

### T5. `docs/reinforcement.md:36-39` 表格数值未统一小数位

表格混用 `.000`（省略前导 0）与 `.111` 样式。与 B3 同一处修复时建议统一为 `0.000`。

---

## 单边更新若干（I18N 专项）

**结构对等性：通过。** 用脚本逐项比对（标题层级与文本、代码围栏、表格行、列表项），两侧均为 **134 个结构项**、`h1`–`h4` 标题均 **37 个**，形状序列完全一致；两侧标题内的行内代码 token 集合逐一相同。**无标题缺失、无段落错位。**

**但内容层面存在单边更新（这才是问题所在）：**

| # | 位置 | 英文侧 | 中文侧 | 判定 |
|---|---|---|---|---|
| D1 | `README.md:381` / `README.zh.md:381` | `maxFactTokens` 已实现 | 「目前未实现」 | **中文侧过时且错误** → B2 |
| D2 | `README.md:68` / `README.zh.md:68` | "(always registered)" | 「（点名工具并指向各自的定义）」 | 英文侧已精简，中文侧保留旧句 |
| D3 | `README.md:75` / `README.zh.md:75` | "the seven fields" | 「七个字段」 | 两侧一致（且正确） |
| D4 | `README.md:346` / `README.zh.md:346` | "Eleven tool schemas" | 「十一个工具 schema」 | 两侧一致（但都错）→ B1 |
| D5 | `README.i18n.yaml:14-15` | — | — | 配对记录失效 → B5 |

- `README.i18n.yaml` 自查：文件内容确认存在，但**其记录的两个哈希与 HEAD 不符**（B5）
- 两侧 README 自 `97afda3`（diagrams 轮）起，最近一次共同改动是 `f06278d`（第二十三轮），此后 `becb131`/`611eb59` 也同时改了双侧；**D1 说明至少有一轮只改了英文侧**——`git log -1 -- README.md` 与 `git log -1 -- README.zh.md` 同为 `f06278d`，故 D1 的分叉发生在更早的轮次且被 `README.i18n.yaml` 的失效记录掩盖了

---

## 已核对一致清单（防回归基线）

以下断言经**双侧取证**（文档 `file:line` ↔ 代码 `file:line`，多数含实跑）确认一致，可作为后续回归基线：

### 配置默认值（`dsh/src/config.ts:210-252` ↔ 三份文档表格）

32 个 schema 字段全部核对。三处表格（`README.md:81-108`、`README.zh.md:81-108`、`dsh/README.md:31-54`）所列默认值**逐项与代码一致**：

`dbPath` `~/.dsh/atom-memory/memory.db`｜`pythonBin` `''`｜`autostart` `true`｜`enabled` `true`｜`captureEnabled` `true`｜`llmExtractionEnabled` `true`｜`contextInjectionEnabled` `true`｜`overviewEnabled` `true`｜`overviewIdleSeconds` `90`｜`overviewRefreshMinutes` `15`｜`injectedSummaryTokens` `800`｜`extractionMaxTokens` `2048`｜`summaryTokens` `1500`｜`nudgeEnabled`/`nudgeIntervalMinutes` `true`/`30`｜`maxRecalledFacts` `10`｜`maxFactTokens` `600`｜`dedupMaxDistance` `0.10`｜`multiValuedPredicates` `[]`｜`writeAckTimeoutMs` `2500`｜`maxVectorDistance` `0.70`｜`minRelevance` `0`｜`maxActiveFacts` `0`｜`rpcTimeoutMs` `30000`｜`scopeEnabled` `true`｜`scope*` `''`

注：`dsh/README.md:31-54` 表格**未列** `writeAckTimeoutMs`/`maxVectorDistance`/`minRelevance`/`maxActiveFacts`/`maxFactTokens`/`dedupMaxDistance` 六项（属覆盖度遗漏，非数值错误；已在 S2 同区域记录）

### 注入预算

- `injectedSummaryTokens` 默认 800 ↔ `dsh/src/injection-budget.ts:19` `DEFAULT_INJECTED_SUMMARY_TOKENS = 800`
- 挡位 `300 / 800 / 1500 / 3000 / 6000 / 12000` ↔ `dsh/src/injection-budget.ts:45` `INJECTED_SUMMARY_TOKEN_PRESETS`（`[300, 800, 1500, 3000, 6000, 12_000]`）——`README.md:265`、`README.zh.md:265`、`docs/python-library.md:178`、`dsh/README.md:45,69` 五处**全部一致**

### 行长度上限（`README.md:394` / `README.zh.md:394` / `docs/python-library.md:199-208`）

- 80 / 40 / 120 ↔ `atom_memory/summary.py:70` `_MAX_COMPACT_LINE_CHARS = 80`、`:76` `_MAX_FOLDED_VALUE_CHARS = 40`、`:80` `_DETAIL_CONTENT_CHARS = 120`、`:86` `_MAX_DETAIL_FIELD_CHARS = 120` — **全部一致**
- `_MAX_OVERVIEW_CHARS` ↔ `atom_memory/summary.py:182` `_MAX_OVERVIEW_CHARS = 1600`，`README.md:336`/`README.zh.md:336` 引用该常量名，**存在且语义相符**

### 检索与排序权重

- `0.4·rrf + 0.2·effective_importance + 0.2·recency + 0.2·trust` ↔ `atom_memory/config.py` 实测 `w_rrf=0.4 / w_importance=0.2 / w_recency=0.2 / w_trust=0.2`；`dsh/src/config.ts` 无覆盖 — `README.md:172`、`README.zh.md:172`、`docs/python-library.md:335`、`docs/scopes.md:189` **一致**
- 作用域三项 `0.20·scope_distance + 0.15·condition_match + 0.05·phase_match` ↔ 实测 `w_scope=0.2 / w_condition=0.15 / w_phase=0.05`；`docs/scopes.md:190,344`、`docs/python-library.md:287-289` **一致**
- `RRF_k = 60` ↔ `atom_memory/retriever.py:87` `RRF_K = 60`，`:143` `rrf_ceiling(k: int = RRF_K)`；`docs/python-library.md:279` `rrf_k: int = 60` **一致**
- `relevance = min(1, rrf/rrf_ceiling)`、ceiling `2/(k+1)` ↔ `docs/python-library.md:337-341` 与 `atom_memory/retriever.py:7,17,163-169` **一致**

### 强化与近期项（`docs/reinforcement.md`）

- `A_MAX=0.5`、`N_HALF=3`、`HALF_LIFE_DAYS=75`、`COOLDOWN_SEC=600` ↔ `atom_memory/reinforce.py:130,135` + 实测 `ReinforceCurve(a_max=0.5, n_half=3.0, half_life_days=75.0, cooldown_sec=600.0)` — **四个参数全对**（仅 A(n) 派生表错，见 B3）
- `KIND_GAINS` 1.0 / 0.8 / 0.6 / 0.0 ↔ `atom_memory/reinforce.py:171-176` — **一致**
- Recency 表（`docs/reinforcement.md:143-149`，半衰期 30 天）：newest 1.000 / +86s 0.999 / +1d 0.977 / +7d 0.851 / +30d 0.500 ↔ 复算 `0.5**(d/30)` = 1.0000 / 1.0000 / 0.9772 / 0.8507 / 0.5000 — **逐格一致**
- `RECENCY_HALF_LIFE_DAYS = 30`、`RECENCY_REFERENCE_WINDOW_DAYS = 3 × 半衰期 = 90` ↔ `atom_memory/retriever.py:106,113` 实测 30.0 / 90.0 — **一致**（文档 `:162-166` "set to 3×" 正确）
- "`memory.md` 半衰期 14 天（vs retriever 30）" ↔ `atom_memory/summary.py:119` `_RECENCY_HALF_LIFE_SECONDS = 14*24*60*60*1000`（注释自承单位是 ms），实测 `12409600000 ms / 86400000 = 14.0 天` — **一致**（`docs/reinforcement.md:173-175`）

### 记忆类型权重（`docs/python-library.md:152-153`）

实测 `models.TYPE_IMPORTANCE`（`atom_memory/models.py`）：

```
{'decision_rule': 0.9, 'lesson': 0.85, 'sop': 0.8, 'procedural': 0.7,
 'semantic': 0.6, 'task': 0.55, 'episodic': 0.5, 'few_shot': 0.5}
```

与文档 `decision_rule 0.90, lesson 0.85, sop 0.80, procedural 0.70, semantic 0.60, task 0.55, episodic/few_shot 0.50` — **八个类型逐一一致**

### 作用域信号可靠性表（`docs/scopes.md:70-80`）

全部 21 个信号 ↔ `atom_memory/context.py:129-157` `SIGNAL_SPECS`：

`explicit_*` 0.95 ✓｜`git_root`/`doc_id`/`folder_id`/`email_thread` 0.90 ✓｜`git_remote` 0.80 ✓｜`org_domain`/`share_link` 0.75 ✓｜`package` 0.55 ✓｜`doc_title`/`project_code`/`path`/`folder_path` 0.50 ✓｜`participants` 0.45 ✓｜`content_anchor` 0.25 ✓｜`name` 0.15 ✓ — **逐项一致**

### 作用域层级（`docs/scopes.md:44`）

`global > user > org > team > client > project > series > phase > document > thread` ↔ `atom_memory/context.py:60-71` `SCOPE_TYPES` — **10 级逐项一致**（`docs/python-library.md:110` 不完整，见 S3）

### 作用域距离权重（`docs/scopes.md:193-194`）

current 1.0 / parent 0.8 / grandparent 0.6 / global 0.5 / another-phase 0.5 / sibling 0.3 / other 0.15 ↔ `atom_memory/scope.py:94-100` `W_PARENT=0.8, W_GRANDPARENT=0.6, W_ANCESTOR=0.5, W_GLOBAL=0.5, W_PHASE=0.5, W_SIBLING=0.3, W_OTHER=0.15` — **一致**；`condition_match` 中性 0.5 ↔ `scope.py:105` `W_CONDITION_NEUTRAL=0.5` — **一致**

### Schema 迁移（`README.md:174` / `README.zh.md:174`）

"thirteen migrations"、"十三个迁移"，`001`–`013` 逐项列名 ↔ `atom_memory/migrations/` 实有 **13 个** `.sql` 文件，编号连续无缺；`PRAGMA user_version` 门控 ↔ `atom_memory/db.py:230-237`

### 策略规则数（`README.md:198` / `README.zh.md:198`）

"the twenty-one policy rules"、"二十一条策略规则" ↔ `docs/memory-semantics.md` 编号标题 `## 1.` … `## 21.` 共 **21** 条

### live-editable 字段数（`README.md:75` / `README.zh.md:75`）

"seven fields"、"七个字段" ↔ `dsh/src/index.ts:549-563` `LiveSettingsSchema` 恰 **7** 个字段（`dsh/README.md` 列举不全，见 S2）

### 文档代码引用可解析性

`docs/*.md`、三份 README 中的相对链接（`../atom_memory/db.py`、`../dsh/README.md`、`dsh/CHANGELOG.md`、`dsh/cordis.patch.yml`、`docs/*.md` 等）**全部解析成功**（`Test-Path` 逐条通过，无失效链接）

### 架构图证据

- 42 条 `sources` 引用（`docs/diagrams/src/*.architecture.json`）**文件存在性与行号范围全部通过**，0 问题
- 三张 `*.visual-check.json` 均为 `"ok":true, "status":"pass"`，含 1440×900 / 2048×1320 深浅两主题视口记录，与 `docs/diagrams/README.md:62` 描述相符（`visualReview:"pending"` 亦与该文 `:71` 的"不能替代人工观感评审"自述一致）
- `docs/diagrams/README.md:64` 的"各 24 条"计数错误（见 S5）

### `docs/python-library.md` quick start 实跑

`docs/python-library.md:41-77` 的代码块**原样执行成功**：`add` × 2 → `recall` 返回 fact 并打印 `subject/predicate/object/final_score` → `summary()`（含 `fact_id`）/`summary(detail=False)`（紧凑）/`user_md()` → 直接读 `mem.db` 取 `fact_id` → `replace()` → `forget()` → `stats()` → `stop()`，全链路无异常。**唯一偏差**是 `stats()` 返回键（S4/B4 同源）

### F09 / F11 复核（上一轮结论在 HEAD 的再验证）

- **F09（`memory_summary` 文本 == 注入文本）**：实测 `rpc._summary({detail:false, overview:true})` 与带 `include_meta:true` 的注入调用返回**同一字符串**（断言 `tool_view == inject_view["text"]` 通过）。代码侧 `dsh/src/tools.ts:1043` 紧凑深度传 `overview: true`，`dsh/src/context.ts:180-184` 注入冻结亦传 `overview: true` + `detail: false`，两路走同一 `summary` RPC。**当前 README 表述成立**
- **F11（空库不注入）**：空库实测 `rpc._summary(include_meta:true)` 返回 `facts = 0` 且 `text` 非空（等于 `summary.py:148` 的 `_EMPTY_NOTICE`）。但注入侧 `dsh/src/context.ts:197-203` 以 `facts === 0` 为判据**短路返回 `''`**，故系统提示词中确实不出现该块。**当前 README:259 表述成立**（其机制是 `context.ts` 的 `facts === 0` 分支，而非 `text` 为空——文档未展开这一点，但结论不假）

---

## 两侧需要改动的归属汇总

| 编号 | 级别 | 需改哪一侧 |
|---|---|---|
| B1 | 阻塞 | 文档（README.md / README.zh.md / dsh/README.md） |
| B2 | 阻塞 | 文档（README.zh.md:381） |
| B3 | 阻塞 | 文档（docs/reinforcement.md:36-39） |
| B4 | 阻塞 | 文档（docs/python-library.md:94） |
| B5 | 阻塞 | 文档（README.i18n.yaml:14-15） |
| S1 | 建议 | 文档（dsh/README.md:126-130） |
| S2 | 建议 | 文档（dsh/README.md:59-60） |
| S3 | 建议 | 文档（docs/python-library.md:110） |
| S4 | 建议 | 文档（docs/python-library.md:88-95,116-117） |
| S5 | 建议 | 文档（docs/diagrams/README.md:64） |
| S6 | 建议 | 文档（docs/python-library.md:257） |
| S7 | 建议 | 文档（docs/python-library.md:260,266） |
| S8 | 建议 | 文档（docs/python-library.md:285） |
| S9 | 建议 | 无需修复（语义一致） |
| T1–T5 | 风格 | 文档 |

**本轮未发现需要改代码的条目**——所有不一致都落在文档侧（代码为正确基准）。其中 B2 是唯一"两侧文档互相矛盾"的条目，优先修。

---

## 未覆盖 / 局限

- `docs/code-review-2026-09-16.md`（66KB）与本轮 task-1/task-2/task-3 的三份报告属历史/并行审核产物，未纳入一致性核对范围（其 `file:line` 引用必然随 HEAD 漂移，属预期）
- 三份设计文档（`docs/dsh-atom-memory 作用域感知记忆系统改进方案.md`、`docs/Domain 维度落地方案（全新数据库基线）.md`、`docs/保留 Scope、新增 Domain 的改进方案*.md`）为**方案稿**性质，其中的 `*.sql:行号`、`api.py:行号` 等引用大量指向尚未落地的设计编号，未作为"应一致的契约"逐条核对；仅抽查了作用域层级与 domain 语义，未发现与实现冲突
- `dsh/CHANGELOG.md`（1101 行）逐轮记录，其中测试计数（如 `:218` "pytest 471 → 486，vitest 245 → 248"）为**当轮快照**，与本轮基线（573 passed / 1 skipped、307 passed）不属于同一时点，**不构成不一致**，故未列为发现。其中 `:220-224` 记录的"lib 与 src 构建漂移"经核实**已修复**（`dsh/lib/index.mjs` 含 `memory_domains`，`git status dsh/lib` 干净）
- 未做在线渲染验证（Markdown → HTML），T1 的破版判断基于 Markdown 表格语法（表头与分隔行缺失）
- 未核对 `docs/diagrams/*.html` 内部文本（700KB 级内联制品）与 JSON 规格的一致性，仅核对了 JSON 规格的引用有效性与 visual-check 证据