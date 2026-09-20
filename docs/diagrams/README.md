# dsh-atom-memory 架构图

三张可交互的独立 HTML 图，基于仓库真实代码绘制：**系统功能结构图**、**数据流程图**、**类与模块结构图**。

每一张都是单文件、零依赖的制品 —— 双击即可在浏览器打开，自带深/浅色主题切换、缩放平移、搜索、聚焦与关系追踪、演示模式，以及 PNG / JPEG / WebP / SVG 导出。

## 成品

| 图 | 文件 | 类型 | 说明 |
| --- | --- | --- | --- |
| 系统功能结构 | [`system-architecture.html`](system-architecture.html) | architecture | 两侧职责、模块划分、进程边界 |
| 数据流程 | [`memory-dataflow.html`](memory-dataflow.html) | dataflow | 写入、检索、派生三条主路径 |
| 类与模块结构 | [`class-structure.html`](class-structure.html) | architecture（类图） | 核心类、两个正交维度、跨进程类 |

三张图都带**导览章节**（左上角），点选即可只高亮该路径，其余淡出。

## 可编辑源码

图形不是手写 SVG，而是由一份小规格 JSON 渲染出来的。改 JSON、重新渲染即可。

```
src/
  system-architecture.architecture.json
  memory-dataflow.dataflow.json
  class-structure.architecture.json
```

用 Archify 重新生成：

```bash
node <archify>/bin/archify.mjs deliver architecture src/system-architecture.architecture.json system-architecture.html --quality showcase --repo-root .
node <archify>/bin/archify.mjs deliver dataflow     src/memory-dataflow.dataflow.json     memory-dataflow.html     --quality showcase
node <archify>/bin/archify.mjs deliver architecture src/class-structure.architecture.json   class-structure.html     --quality showcase --repo-root .
```

`--repo-root` 只在图里含 `sources` 代码引用时需要，用来核对引用路径与行号确实存在于所固定的 revision。dataflow 类型不接受这个参数。

## 图里说了什么

### 系统功能结构

从上到下五层：会话与面板 → 插件装配层 → 功能模块 → 桥接层 → Python 侧。两条区域边界就是**进程边界**：`dsh 宿主侧` 全部跑在 Node 里，`Python 库侧` 跑在独立子进程里，中间只以 NDJSON 行协议通信。

三张卡片各讲一件事：两侧职责如何切分、策略归属在哪一侧、进程边界换来了什么。

### 数据流程

五条泳道对应五个阶段，主要看三条路径：

- **写入**：用户消息 → LLM 抽取 → typed candidates → 校验裁决 → 作用域与主题 → 向量化 → facts 单事务落库。
- **检索**：查询同时打全文与向量两条索引，RRF 融合后加权重排。
- **派生**：活跃事实聚合成工作单元，空闲时合成综述，写回缓存供下一次会话读取。

数据流程图里不含 `meta.repository`，所以没有逐行代码引用 —— Archify 的仓库证据校验只支持 architecture 类型，这里如实不带。

### 类与模块结构

Archify 没有独立的 `class` 图类型，因此类图用 architecture 类型表达：每个组件是一个类或模块，连线是「持有 / 调用 / 构造 / 投影」这类关系。卡片里说明了为什么全库几乎不用继承，以及两个正交维度（`ScopeStore` 的作用域、`DomainStore` 的主题）为什么各自成类。

## 校验与证据

三张图都通过 Archify 的 `showcase` 质量档：**9 / 9 检查项，0 error，0 warning**，并在无头 Chrome 里通过 `visual-check` 的视口容纳、可读性与导航镀铬三项检查（1440×900、1600×1000、1920×1080、2048×1320，深色与浅色两种主题）。

两张 architecture 图另带**仓库源码证据**（各 24 条引用），逐条核对了文件存在性与行号。

```bash
node <archify>/bin/archify.mjs validate architecture src/system-architecture.architecture.json --quality showcase --repo-root . --json
node <archify>/bin/archify.mjs visual-check system-architecture.html --json
```

> `deliver` 做的是确定性制品校验（规格字节固定、原子写入、SHA-256 回执）；`visual-check` 是真实浏览器里的有界行为证据（布局是否溢出一屏、缩放后的文字是否仍可读、控件是否互相遮挡）。两者都是机器判据，不能替代人工的观感评审。