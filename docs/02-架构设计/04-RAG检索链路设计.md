# RAG 检索链路设计

> 状态：现行 · 最后校验：2026-08-21 · 校验方式：符号级 + 行为实测（`scripts/eval_rag.py` 16 条基线 + `scripts/verify_rag_features.py` 8 项断言）
> 本页讲**为什么这么设计**（检索链路选型与权衡）；功能面（怎么用、配置、API）见 [02-RAG知识库.md](../01-核心功能/02-RAG知识库.md)。

***

## 1. 设计目标

企业知识库检索要同时满足四个指标，单路召回做不到：

| 指标        | 含义       | 单路向量 / 单路关键词的短板          |
| --------- | -------- | ------------------------ |
| recall\@1 | 首个命中是否相关 | 向量对术语/编号型查询弱；关键词对口语长句弱   |
| MRR       | 命中排位     | 单路排序质量有限                 |
| nDCG\@3   | 前 3 相关性  | 同上                       |
| 可用性       | 离线/降级可演示 | embedding/rerank 外部依赖会断链 |

此外，真实问答还有三个单块检索覆盖不到的场景，由本轮新增能力补齐：

- **答案横跨分块边界** → 上下文窗口扩展（前后邻居拼接）；
- **查询措辞与文档表述差异大、一个问句含多个子主题** → 多路召回（多 query 变体并行）；
- **查询信息不足/无命中** → 模糊澄清（Agent 主动向用户追问，而不是硬答）。

## 2. 链路选型与权衡

```
query ──多路变体(可选)──► ┌─ 向量召回(LanceDB) ─┐
   原句/改写/关键词/子问题 ─┤                   ├─► RRF 融合 ──► rerank 精排 ──► top_k
                           └─ 关键词召回(FTS5+LIKE) ┘
   命中结果：content(子块) + parent(父块) + context(邻居窗口,可选) + 澄清信号(无命中/低置信时)
```

| 环节    | 选型                                                                       | 为什么                                               | 降级策略                                      |
| ----- | ------------------------------------------------------------------------ | ------------------------------------------------- | ----------------------------------------- |
| 多路召回  | **多 query 变体并行**（默认关）：原句 + 规则关键词 + LLM 改写 + LLM 子问题，各变体独立走两路后统一 RRF 合并   | 单 query 的两路召回对"措辞差异大/多主题复合问句"覆盖面有限；变体间天然互补        | 规则关键词零成本必出；LLM 变体失败跳过；至少保留原句              |
| 向量召回  | LanceDB（本地文件，0 服务）                                                       | 单机零成本、无外部 DB 依赖                                   | 不可用 → 空召回，走纯关键词                           |
| 关键词召回 | SQLite FTS5 + LIKE 兜底                                                    | 与业务 DB 同源，术语/编号型查询强                               | LIKE 永远可用                                 |
| 融合    | RRF（倒数排名融合）                                                              | 无需调参、对分数尺度不敏感                                     | 固定权重 60/40 → 自适应（开关）                      |
| 精排    | OpenRouter rerank → Ollama 探测 → 保持 RRF                                   | 精排是 recall 提升最大的单一手段（实测 +6.2%）                    | 探测不可用 → `skipped` 保持 RRF 顺序，绝不中断链路        |
| 改写    | LLM 查询改写（默认关）                                                            | 口语化 → 检索友好关键词；有额外 LLM 成本                          | 失败回落原文                                    |
| 上下文窗口 | 命中块 ±N 邻居拼接（`RAG_CONTEXT_WINDOW`，默认 1）                                   | 分块保证检索精度，窗口补齐上下文完整度，两者解耦                          | 单块文档无邻居时 context==content；读取失败降级无 context |
| 模糊澄清  | `search_kb` 返回 `needs_clarification` + 澄清问题；KnowledgeAgent 提示词要求输出问题而非硬答 | 无命中/低置信时硬答 = 幻觉来源；追问与既有 `POST /messages` 多轮链路天然衔接 | 开关关 → 退回"如实告知"                            |

**关键设计决策**：

1. **small-to-big 分块**：子块（500）做命中单位保证精确，父块（1500）做返回上下文保证完整；章节 `path` 元数据支持定位。**子块→父块的关联采用"按字符偏移贪心匹配"，写入索引时一次性固化，检索期零计算**（详见下方专节）。

### 2.1 子块 ↔ 父块关联机制（small-to-big 的"子"与"父"怎么绑定）

**两套粒度是独立切块，不是嵌套拼接**：同一篇文档分别用 `VECTOR_CHUNK_SIZE=500`（子块，进向量索引做检索单位）和 `VECTOR_PARENT_CHUNK_SIZE=1500`（父块，命中时带回的大上下文；`<=0` 时整篇作单个父块）各切一遍，见 `app/vector_kb.py:_chunk_text` / `_parent_blocks`。

**关联算法（建索引时一次算好）**——`_build_rows` 对每个子块：

```python
# ① 用子块开头 30 字符在全文定位大致偏移（近似定位，非精确）
off = content.find(child[:30])
# ② 把子块归入"覆盖该偏移"的父块
parent = _pick_parent(parents, content, child, off)
```

`_pick_parent`（`app/vector_kb.py`）按**父块累计长度 + 子块偏移落在哪个区间**做贪心匹配：

```python
def _pick_parent(parents, full, child, off):
    if len(parents) <= 1:
        return parents[0] if parents else ""   # 仅 1 个父块：直接归它
    if off < 0:
        return parents[0]                      # 定位失败：保守归第一个
    acc = 0
    for p in parents:                          # 父块按顺序累计字符长度
        acc += len(p)
        if off < acc:                          # 子块偏移落进该父块区间 → 归它
            return p
    return parents[-1]                          # 落点在最后 → 归最后一个
```

关联结果直接写入向量表的 `parent` 列（每条记录同时含 `content` 子块 / `parent` 父块 / `path` 章节路径），**检索时只取列、不再回看文档原文**。

**返回侧切换**（`app/kb.py:_rrf_merge`）：

```python
body = r.get("parent") or r["content"] if settings.return_parent_chunk else r["content"]
```

`RAG_RETURN_PARENT=true` → 命中子块返回其 `parent` 大上下文（空时兜底子块自身）；默认 false → 返回子块 `content`。

**边界与 trade-off**：`content.find(child[:30])` 返回**首个**匹配位置，若多个章节大量重复开头句式（如"操作步骤如下"），子块可能被归到错误的父块。这是有意简化——比精确分块 O(n) 重得多，对绝大多数文档足够；若特定语料归块偏差明显，可改为"按切块序号对齐"（子块落入其区间所在的父块）。
2\. **上下文窗口与分块正交**：块内顺序号 `doc_idx` + 文档键 `doc_key` 落库（随 `upsert_docs`/`index_docs` 自动维护），命中后按 `doc_idx±N` 取同文档邻居拼接为 `context`；不改分块策略、不重训向量，纯检索侧增强，0 开关时代码路径与原链路一致。
3\. **多路召回默认关**：与 query 改写同成本考量（规则关键词变体零成本、常驻；仅 LLM 改写/子问题动模型）；开启后任何 LLM 失败自动跳过该变体，至少保留原句——多路是"召回面"的加法，不是链路风险点。
4\. **澄清是信号不是状态机**：不新增工单状态，`search_kb` 返回结构化信号 + 提示词约束，Agent 的输出结论即"提问"，用户回答走既有 followup 触发新一轮——与系统异步消息模型零冲突。
5\. **增量维护**：`add_document`/`delete_document` 走向量 upsert/delete + FTS 行级同步，不整库重建（重建保留给 seed/脚本）。
6\. **维度防漂移**：embedding 主/兜底模型维度不一致直接报错（`VECTOR_EMBED_DIM_CHECK`），防悄悄写坏索引。
7\. **ANN 延迟开启**：小库精确扫描更快更准，`VECTOR_ANN_ENABLED=false` 默认；数据量上来再开 IVF\_PQ/HNSW\_SQ。
8\. **评测先行**：16 条人工标注查询作为回归基线（`scripts/eval_rag.py --compare / --multi on`），`scripts/verify_rag_features.py` 对三块新能力做 8 项断言——**检索质量可量化，不靠感觉**。

<br />

<br />

<br />

## 3. 实测基线（16 条查询，2026-08-18 随播种数据对齐后重测）

| 指标        | rerank 关 | rerank 开 | 提升    |
| --------- | -------- | -------- | ----- |
| recall\@1 | 0.938    | 1.000    | +6.2% |
| recall\@3 | 1.000    | 1.000    | —     |
| MRR       | 0.969    | 1.000    | +3.1% |
| nDCG\@3   | 0.977    | 1.000    | +2.3% |

多路召回对比（`scripts/eval_rag.py --multi on`）：当前 16 条评测集上开启与关闭均为全 1.000（小评测集 baseline 已满分，不劣化即通过；多路价值体现在措辞差异大的长尾场景）。三块新能力另有 8 项断言（`scripts/verify_rag_features.py`，全过）覆盖：窗口邻居拼接、多路变体生成与去重、澄清信号触发与开关关闭。

> 复现：`python scripts/seed_demo.py && python scripts/eval_rag.py --compare && python scripts/verify_rag_features.py`（需 embedding/rerank 可用；离线自动降级关键词路）。评测集与 `scripts/seed_demo.py` 的 5 篇种子文档对齐（2026-08-18 曾因旧评测集引用已下线文档导致全 0，已修复）。

## 4. 与 Agent 的衔接

- `search_kb`/`search_kb_direct`（low，只读）挂 KnowledgeAgent——工具面见 [01-企业级Agent-Harness.md](../01-核心功能/01-企业级Agent-Harness.md)；
- 模糊澄清：`search_kb` 返回 `needs_clarification`/`clarify_questions` 时，KnowledgeAgent 输出澄清问题等待用户补充（用户经 `POST /messages` 走既有 followup 多轮链路，无需新增状态）；
- 检索上下文进入「上下文装配」的选/压/截流程（`app/context_assembler.py`）；
- 知识库管理 API（upload/delete/list）面向 operator/admin，前端 `web/app/kb/page.tsx`。

## 5. 边界与后续方向

- 当前 embedding 主用依赖 OpenRouter（`.env.example` 空值陷阱见 [配置清单.md](../03-部署运维/配置清单.md) 陷阱 1）；完全离线需本地 Ollama bge-m3；
- 多路召回默认关（LLM 成本考量）：生产环境可按场景评估开启，配合 `RAG_MULTI_QUERY_MAX_VARIANTS` 控成本；
- 澄清阈值 `RAG_CLARIFY_THRESHOLD` 目前基于 rerank top1 分数，后续可引入"检索多样性"（top1 与 top2 差距）等次级信号；
- 后续可选：混合检索权重在线学习、rerank 模型替换、知识库权限分级（tag 已支持业务线隔离）。

<br />

# 补充：FTS = Full-Text Search（全文检索）

在你的这份文档里，FTS 指的是 **SQLite FTS5 全文检索扩展**，它是**关键词召回路**的核心实现（对应文档表格里 `app/kb.py:_fts`）。

### 1. 在本项目里是怎么落地的

建表语句在 [kb.py:L19-21](file:///Users/lijia/Documents/02.Coder/企业级混合Agent工作流Demo/app/kb.py#L19-L21)：

```python
FTS_SCHEMA = """
CREATE VIRTUAL TABLE IF NOT EXISTS kb_fts USING fts5(title, content, tag);
"""
```

关键点：`kb_fts` 不是普通表，而是 **FTS5 虚拟表**。写入时照常 `INSERT`（[kb.py:L79-81](file:///Users/lijia/Documents/02.Coder/企业级混合Agent工作流Demo/app/kb.py#L79-L81)），但 SQLite 内部会为 `title` / `content` / `tag` 三列建立**倒排索引**（分词 → 词项 → 文档列表），从而支持快速的全文匹配与相关性打分。

查询时用 FTS5 特有的 `MATCH` 子句 + `bm25()` 排序（[kb.py:L222-226](file:///Users/lijia/Documents/02.Coder/企业级混合Agent工作流Demo/app/kb.py#L222-L226)）：

```python
sql = ("SELECT title, content, tag, bm25(kb_fts) AS rank FROM kb_fts "
       "WHERE kb_fts MATCH ?")
```

- `MATCH ?`：全文匹配语法，支持 `"VPN"` 短语、`开通 AND 流程` 布尔组合等多种查询表达式；
- `bm25(kb_fts)`：**BM25 经典相关性评分函数**（词频 + 逆文档频率的加权打分），`rank` 值越小越相关（默认是负分，`ORDER BY rank` 升序取最优）。

### 2. FTS5 vs. 普通 LIKE 的本质区别

| 维度   | FTS5（`_fts`）       | LIKE（`_like` 兜底） |
| :--- | :----------------- | :--------------- |
| 索引方式 | 倒排索引，检索速度快，可打分排序   | 全表逐行子串扫描         |
| 相关性  | 有 BM25 分数          | 无，只按命中           |
| 匹配语义 | 按"词项"匹配（短语、布尔、前缀等） | 纯字符子串包含          |

### 3. 中文场景的一个关键坑（为什么要有 LIKE 兜底）

代码注释写得很清楚（[kb.py:L118](file:///Users/lijia/Documents/02.Coder/企业级混合Agent工作流Demo/app/kb.py#L118)）：

> 中文场景 FTS5 MATCH 常为空串，故关键词路对中文用 2-gram LIKE 兜底。

FTS5 默认的 `unicode61` 分词器是按**空格/标点切分英文词**的，对连续中文文本基本不分词，因此 `MATCH '查VPN开通流程'` 这类整句中文查询很可能返回空。所以检索链路设计成：

```
_keyword_hits → 先 _fts（FTS5 精确/英文/术语命中）
               → 无结果再 _like（中文拆词 + 2-gram 子串匹配兜底）
```

见 [kb.py:L145-151](file:///Users/lijia/Documents/02.Coder/企业级混合Agent工作流Demo/app/kb.py#L145-L151)。

### 4. 在整条检索管线中的位置

对应文档第 1 节的图：

```
query ──► ┌─ 向量召回(LanceDB) ┐
          └─ 关键词召回(FTS5 + LIKE 兜底) ┘──► RRF 融合 ──► rerank ──► top_k
```

FTS5 解决的问题是**术语 / 精确编号 / 专有名词**这类"必须字面命中"的检索（如 `502`、`bge-m3`），与向量召回（解决同义/口语化）互补，两路结果再进入 RRF 根据**排名**而非原始分值融合。当向量/embedding 不可用时，整条链路还会自动降级为纯 FTS5 关键词路（`🔌` 模式）。

此外 FTS 表还承担了两个"计数器"职责：应用启动时用它判断知识库是否为空来决定是否播种（[main.py:L56](file:///Users/lijia/Documents/02.Coder/企业级混合Agent工作流Demo/app/main.py#L56)），文档增删时对 `kb_fts` 做行级同步保证幂等（[kb.py:L79-L81](file:///Users/lijia/Documents/02.Coder/企业级混合Agent工作流Demo/app/kb.py#L79-L81)、[kb.py:L99-L100](file:///Users/lijia/Documents/02.Coder/企业级混合Agent工作流Demo/app/kb.py#L99-L100)）。

简单总结：**FTS = Full-Text Search，即 SQLite 内置的 FTS5 全文检索扩展，是这套 RAG 里"关键词召回路"的引擎**，负责术语级精确命中；中文整句检索靠 `_like` 的 2-gram 兜底补齐。

# 补充：当前 rerank：**从** **`top_k × 4`** **精排到** **`top_k`（默认 top\_12 → top\_3）**

整条管线的流程可以拆成 4 步，关键参数和代码位置都标出来了：

### 1. 关键配置默认值

| 参数                             | 默认值      | 位置                                                                                                  |
| :----------------------------- | :------- | :-------------------------------------------------------------------------------------------------- |
| `VECTOR_TOP_K`（最终输出条数）         | **3**    | [config.py:L133](file:///Users/lijia/Documents/02.Coder/企业级混合Agent工作流Demo/app/config.py#L133)       |
| `RERANK_RECALL_FACTOR`（召回放大倍数） | **4**    | [config.py:L91-L92](file:///Users/lijia/Documents/02.Coder/企业级混合Agent工作流Demo/app/config.py#L91-L92) |
| `RERANK_ENABLED`               | **true** | [config.py:L81](file:///Users/lijia/Documents/02.Coder/企业级混合Agent工作流Demo/app/config.py#L81)         |

### 2. 每一步的"top"数值

核心逻辑在 [kb.py:search](file:///Users/lijia/Documents/02.Coder/企业级混合Agent工作流Demo/app/kb.py#L115) 里：

```
① 召回候选量  recall_k = top_k × RERANK_RECALL_FACTOR
                   = 3 × 4 = 12      （kb.py:L129）

② 两路召回各自取 recall_k=12
   向量路 _vector_hits(q, recall_k=12)          （kb.py:L134）
   关键词路 _keyword_hits(q, recall_k=12)       （kb.py:L135）

③ RRF 融合后再裁到 recall_k=12 个候选
   merged = _rrf_merge(..., recall_k=12)        （kb.py:L136）

④ rerank 精排：输入 12 条 → 输出只取 top_k=3 条
   merged, _ = rerank.rerank(query, merged, top_k=3)   （kb.py:L141）
   → 内部 _apply(..., top_k=3) 在精排后截取前 3 条     （rerank.py:L68-L79）

⑤ 保险：最终再切片一次 return merged[:top_k]   （kb.py:L142）
```

对应源码最关键几行：

- [kb.py:L129](file:///Users/lijia/Documents/02.Coder/企业级混合Agent工作流Demo/app/kb.py#L129)：`recall_k = top_k * settings.rerank_recall_factor if settings.rerank_enabled else top_k`
- [kb.py:L141](file:///Users/lijia/Documents/02.Coder/企业级混合Agent工作流Demo/app/kb.py#L141)：`merged, _ = rerank.rerank(query, merged, top_k)`
- [rerank.py:L77](file:///Users/lijia/Documents/02.Coder/企业级混合Agent工作流Demo/app/rerank.py#L77)：`if len(out) >= top_k: break`

### 3. 两种情景下的实际数值

| 情景                            | 召回路取多少                       | RRF 后候选   | rerank 输入 | rerank 输出（最终） |
| :---------------------------- | :--------------------------- | :-------- | :-------- | :------------ |
| **默认（rerank=开）**              | 向量 12 + 关键词 12（多路召回时每条变体都加量） | 裁到 **12** | **12**    | **3**         |
| **rerank 关闭 / 不可用降级 skipped** | 向量 3 + 关键词 3                 | 裁到 **3**  | 不跑 / 原序返回 | **3**         |

> 注意：如果调用者显式传了 `top_k`（比如 Agent 工具 `search_kb(query, top_k=5)`），那整条链路就按传入值缩放，即 5×4=**20** 条进 rerank，最后精排到 **5** 条。默认不传时才是 `VECTOR_TOP_K=3`（[kb.py:L112](file:///Users/lijia/Documents/02.Coder/企业级混合Agent工作流Demo/app/kb.py#L112)、[skills/kb.py:L41](file:///Users/lijia/Documents/02.Coder/企业级混合Agent工作流Demo/app/skills/kb.py#L41)）。

### 4. 设计意图一句话

先**放宽召回**（×4 倍，宁可多带 9 条低相关也别漏），再用 rerank 模型做**相关性精排截回**，用少量计算成本换 recall 不丢；你文档里 §1 "返回"那行写的「召回候选先放大 `RERANK_RECALL_FACTOR`（默认 4）倍再精排取 top\_k」就是这个机制。

用户询问向量库的增量维护和全量重建分别如何使用。我需要深入查看相关代码，包括：

1. `add_document` / `delete_document` 增量维护函数
2. `rebuild_indexes` 全量重建函数
3. `upsert_docs` / `delete_docs` 向量库操作
4. `scripts/index_kb.py` 脚本
5. 管理 API `app/api/kb.py`

让我先并行读取这些关键文件。

# 补充：向量库的生命周期

好，下面把本项目**向量库（LanceDB，`data/vector-kb`）的两种生命周期操作——增量维护**与全量重建——从「使用入口、代码内部流程、适用场景、注意事项/坑」四个维度彻底讲清楚。

***

## 一、两者定位总览

| 维度           | 增量维护（add / delete）                                                                                                                                                                              | 全量重建（rebuild / index\_docs）                                                                                                                                                                                                                 |
| :----------- | :---------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- | :------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------ |
| 权威入口         | `app.kb.add_document` / `app.kb.delete_document`                                                                                                                                                | `app.kb.rebuild_indexes` → `app.vector_kb.index_docs`                                                                                                                                                                                       |
| 对外入口         | 管理 API `POST /api/kb/upload`、`DELETE /api/kb/{id}`；前端 `web/app/kb/page.tsx`                                                                                                                     | 脚本 `scripts/index_kb.py`、`scripts/seed_demo.py`（内部调用 `seed_kb → rebuild_indexes`）                                                                                                                                                           |
| 对现有数据影响      | **只动目标标题的 chunk，其它文档零触碰**                                                                                                                                                                       | **整个向量表** **`mode="overwrite"`，先清空再写**；FTS 也全量清空重插                                                                                                                                                                                          |
| 必需 embedding | 是（仅对新增/变更的文档切块、embedding）                                                                                                                                                                       | 是（对知识库全部文档重切 + 重 embedding）                                                                                                                                                                                                                 |
| 主要开销         | 低 / 局部                                                                                                                                                                                          | 高 / 全量                                                                                                                                                                                                                                      |
| 典型场景         | 日常新增一篇制度、删除一篇过时 SOP                                                                                                                                                                             | 换 embedding 模型 / 改分块参数 / 数据批量导入 / 索引损坏修复                                                                                                                                                                                                    |
| 代码入口行        | 增 [kb.py:L70-L89](file:///Users/lijia/Documents/02.Coder/企业级混合Agent工作流Demo/app/kb.py#L70-L89) · 删 [kb.py:L92-L107](file:///Users/lijia/Documents/02.Coder/企业级混合Agent工作流Demo/app/kb.py#L92-L107) | 索引 [vector\_kb.py:L300-L315](file:///Users/lijia/Documents/02.Coder/企业级混合Agent工作流Demo/app/vector_kb.py#L300-L315) · 脚本 [scripts/index\_kb.py:L24-L41](file:///Users/lijia/Documents/02.Coder/企业级混合Agent工作流Demo/scripts/index_kb.py#L24-L41) |

***

## 二、增量维护（日常使用推荐）

本项目是"三存储同步"——`kb_documents`（SQLite，权威来源）+ `kb_fts`（FTS5 虚拟表，关键词路）+ LanceDB 向量表（向量路）。增量意味着三条链路**只改目标文档的行/块**。

### 2.1 怎么用：三种入口

#### 入口 A：管理 API（operator/admin 权限）——前端/运维最常用

```bash
# ① 上传新增文档（PDF/Word/txt/md，10MB 上限）
curl -X POST http://localhost:8000/api/kb/upload \
  -F "file=@/path/to/新员工VPN开通流程.pdf" \
  -H "X-Auth-Token: <operator或admin的token>"

# ② 删除文档（先 GET /api/kb 查出 doc_id）
curl -X DELETE http://localhost:8000/api/kb/7 \
  -H "X-Auth-Token: <operator或admin的token>"
```

API 内部直接调用 `kb_mod.add_document(...)` / `kb_mod.delete_document(doc_id)`，见 [api/kb.py:L74-L102](file:///Users/lijia/Documents/02.Coder/企业级混合Agent工作流Demo/app/api/kb.py#L74-L102)。

#### 入口 B：Python 代码直接调用（写脚本/集成测试）

```python
from app.kb import add_document, delete_document, list_documents

# 新增：返回新文档 id
doc_id = add_document(
    filename="新员工VPN开通流程.pdf",   # 原始文件名，可任意
    title="新员工VPN开通流程",          # ⚠️ 这是向量/FTS 用来去重的"键"，同 title 会被覆盖
    content=open("vpn.txt").read(),     # 纯文本内容（PDF/Word 需先解析）
    tag="network",                      # 业务线标签，检索时可按 tag 过滤
)
print("新增文档 id =", doc_id)

# 删除：按数据库主键 id（不是 title），返回是否命中
ok = delete_document(doc_id)
print("删除结果 =", ok)
```

#### 入口 C：FastAPI shell / notebook

和 B 一样，先 `from app.db import init_db; init_db()` 连上库即可。

### 2.2 内部流程（逐行对应）

**新增 add\_document（[kb.py:L70-L89](file:///Users/lijia/Documents/02.Coder/企业级混合Agent工作流Demo/app/kb.py#L70-L89)）**：

```
① SQLite 主库 INSERT kb_documents → 拿到 doc_id
     ↓
② FTS5 增量：DELETE kb_fts WHERE title=? → INSERT kb_fts
   （先删后插：同 title 再跑一次 = 幂等更新内容，不会重复）
     ↓
③ 向量增量：vector_kb.upsert_docs([{title,tag,content}])
   ├─ _build_rows(docs)            → 小到大切分（子块 500 / 父块 1500），生成 doc_idx
   ├─ embed(texts)                 → 主 OpenRouter，不行走 Ollama bge-m3
   ├─ _check_dim                   → 维度防漂移（不一致直接报错，防写坏）
   ├─ DELETE FROM vector WHERE title = '新员工VPN开通流程'
   └─ tbl.add(rows)                → 写入本次文档的 N 个 chunk
```

关键点看 `upsert_docs`（[vector\_kb.py:L318-L343](file:///Users/lijia/Documents/02.Coder/企业级混合Agent工作流Demo/app/vector_kb.py#L318-L343)）：**不是整库重建，而是先按** **`title`** **删除这篇文档对应的旧 chunks，再插入新 chunks**——其它文档的向量一条都不动。

**删除 delete\_document（[kb.py:L92-L107](file:///Users/lijia/Documents/02.Coder/企业级混合Agent工作流Demo/app/kb.py#L92-L107)）**：

```
① SELECT title FROM kb_documents WHERE id=?
     ↓（没命中直接 return False）
② DELETE kb_documents + DELETE kb_fts（按 title）
     ↓
③ vector_kb.delete_docs([title]) → 按 title 删除向量表中该文档全部 chunk
```

### 2.3 增量维护的注意事项（坑）

1. **`title`** **是增量 upsert 的"去重键"**
   同 title 再 `add_document` 一次 → 实际是**覆盖更新**（FTS 和向量都会先 DELETE 旧的再 INSERT 新的）。想真正新增一篇，务必换一个唯一 title。对应：
   - [kb.py:L79](file:///Users/lijia/Documents/02.Coder/企业级混合Agent工作流Demo/app/kb.py#L79) `DELETE FROM kb_fts WHERE title=?`
   - [vector\_kb.py:L336-L341](file:///Users/lijia/Documents/02.Coder/企业级混合Agent工作流Demo/app/vector_kb.py#L336-L341) 向量表同逻辑
2. **三条链路的一致性边界**
   - 主库 + FTS：都在 SQLite 里，各自事务独立执行（没有跨库事务），如果第 ② 步 FTS 成功但第 ③ 步 embedding 网络异常：
     - 行为：向量 upsert 里 `try/except pass`（[kb.py:L86-L87](file:///Users/lijia/Documents/02.Coder/企业级混合Agent工作流Demo/app/kb.py#L86-L87)）**不回滚主库/FTS**，仅记日志；下次全量重建时会补上。
     - 影响：该文档暂时只有关键词路能搜到，向量路搜不到。
   - 如果这个一致性对你很关键，可以在失败时显式再跑一次全量重建脚本补齐（见下一节）。
3. **doc\_idx 上下文窗口——跨文档无影响**
   增量新增/更新只会对该文档内部的 chunk 重新编号 `doc_idx`（见 `_build_rows` 每篇从 `doc_i=0` 开始 [vector\_kb.py:L247](file:///Users/lijia/Documents/02.Coder/企业级混合Agent工作流Demo/app/vector_kb.py#L247)），**不影响其他文档的邻居关系**，这就是为什么上下文窗口特性可以安全地增量维护。
4. **如果向量库还不存在（首次）**
   `upsert_docs` 有个很贴心的回退：当 `_VECTOR_OK` 为 True 但表还没建时（[vector\_kb.py:L323-L324](file:///Users/lijia/Documents/02.Coder/企业级混合Agent工作流Demo/app/vector_kb.py#L323-L324)），直接调 `index_docs(docs)` 把当前传入的这几篇做成初始向量库——所以"首篇文档用 add\_document 添加"也是可行的，不一定必须先跑 seed。

***

## 三、全量重建（特殊场景使用）

全量 = 以 `kb_documents` 为权威来源，把 `kb_fts` 和向量表**整个推倒重来**。对应两个入口：

### 3.1 怎么用：两种入口

#### 入口 A：独立脚本（运维/改参数后）—— `scripts/index_kb.py`

```bash
# ⚠️ 前置：需要 embedding 可用（主用 OpenRouter key；或 Ollama 启动 + ollama pull bge-m3）
# 先确保 SQLite 里已经有文档（如果是首次，先 seed）：
.venv/bin/python scripts/seed_demo.py

# ① 对当前 SQLite 知识库内容做【全量向量索引重建】
#   （kb_fts 不重建，只重建向量表；适合"只改了 embedding/分块参数"）
.venv/bin/python scripts/index_kb.py

# ② 自定义向量库输出目录（默认 data/vector-kb）
VECTOR_DB_PATH=/tmp/test-vk .venv/bin/python scripts/index_kb.py
```

脚本实现见 [scripts/index\_kb.py:L24-L41](file:///Users/lijia/Documents/02.Coder/企业级混合Agent工作流Demo/scripts/index_kb.py#L24-L41)。它的动作非常克制：只做 `vector_kb.index_docs(docs_from_kb_fts)`，不动 SQLite 主库、不动 FTS。

#### 入口 B：seed\_kb / rebuild\_indexes（Python 代码 / 全新初始化）

```python
from app.db import init_db
from app.kb import init_kb, rebuild_indexes, seed_kb

init_db(); init_kb()

# 场景 1：SQLite 主库已经有文档，FTS + 向量都重跑
rebuild_indexes()   # 对应 kb.py:L44-L59

# 场景 2：全新环境，先把一批文档写入主库再建索引（和 seed_demo.py 内部一致）
seed_kb([
    {"title":"VPN开通","content":"...","tag":"network"},
    ...
])
# seed_kb 内部 = 清空 kb_documents → INSERT → rebuild_indexes（清空 kb_fts 重插 + index_docs 全量向量）
```

### 3.2 内部流程（逐行对应）

**`rebuild_indexes()`（[kb.py:L44-L59](file:///Users/lijia/Documents/02.Coder/企业级混合Agent工作流Demo/app/kb.py#L44-L59)）**：

```
① docs = list_documents()                 取 kb_documents 全量（权威）
     ↓
② DELETE FROM kb_fts; 遍历 INSERT         FTS 全量重建
     ↓
③ vector_kb.index_docs(vec_docs)          向量全量重建
```

**`index_docs()`（[vector\_kb.py:L300-L315](file:///Users/lijia/Documents/02.Coder/企业级混合Agent工作流Demo/app/vector_kb.py#L300-L315)）**：

```
① _build_rows(docs)                       小→大切块 + doc_idx 全局重排 + 父块匹配 + path 提取
     ↓
② embed(texts)                            对所有子块 content 批量 embedding
     ↓
③ _check_dim                              维度防漂移
     ↓
④ vector 归一化写入
     ↓
⑤ ctx.create_table(_TABLE, data=rows, mode="overwrite")
                                             ⚠️ overwrite：旧表直接被覆盖（等价清空+重写）
     ↓
⑥ _maybe_create_ann_index(tbl)            数据≥1000 行且开了 VECTOR_ANN_ENABLED 才建 ANN
```

### 3.3 什么时候必须全量重建（非常实用的判断标准）

**日常增删文档一律用增量**。出现以下任一情况才全量：

| 场景                     | 例子                                                                                                                   | 推荐入口                                                                                    |
| :--------------------- | :------------------------------------------------------------------------------------------------------------------- | :-------------------------------------------------------------------------------------- |
| 🧩 分块参数改了              | 把 `VECTOR_CHUNK_SIZE` 从 500 调到 800；或调 `VECTOR_CHUNK_OVERLAP`；或改 `VECTOR_PARENT_CHUNK_SIZE`（small-to-big 命中时带回的上下文大小） | `index_kb.py`（向量 alone）或 `rebuild_indexes()`                                            |
| 🧠 换了 embedding 模型     | `EMBEDDING_MODEL` 从 `nvidia/nemotron-3-embed-1b` 切到其它；或主/兜底模型变了导致**维度变了**（此时 `_check_dim` 会直接报错拒绝增量，逼你全量重建）          | `index_kb.py`                                                                           |
| 📦 批量导入 10+ 篇          | 一次性把历史 SOP 全量导入                                                                                                      | 写脚本循环 `add_document`（增量）也行；但 50+ 篇以上建议直接写主库 → `rebuild_indexes()`（一次 embedding 批处理更省时间） |
| 🧹 索引损坏 / 怀疑向量与主库不一致   | 删了文档但向量删除报异常；或发现 search 结果里有"标题已不存在"的 chunk                                                                          | `rebuild_indexes()` 最省事                                                                 |
| 🧪 评测基线跑 `eval_rag.py` | 评测前通常会 seed 5 篇 demo 文档（`scripts/seed_demo.py` 内部走 `seed_kb → rebuild_indexes`）                                      | `seed_demo.py` + `eval_rag.py --compare`                                                |

### 3.4 全量重建的注意事项（坑）

1. **overwrite = 旧表直接被替换**
   `create_table(..., mode="overwrite")` 是 LanceDB 的语义（[vector\_kb.py:L313](file:///Users/lijia/Documents/02.Coder/企业级混合Agent工作流Demo/app/vector_kb.py#L313)）：全量重建过程中如果中途崩了（比如 embedding 网络超时），**旧向量表可能已经没了**。建议重要环境先把 `data/vector-kb` 拷一份再跑。
2. **小库（<1000 chunk）默认不建 ANN**
   你文档里写的 `VECTOR_ANN_ENABLED=false`（精确扫描）是默认行为。就算你手动改成 true，`_maybe_create_ann_index` 里 `<1000` 也会跳过并打日志（[vector\_kb.py:L280-L297](file:///Users/lijia/Documents/02.Coder/企业级混合Agent工作流Demo/app/vector_kb.py#L280-L297)）。**5 篇 demo 文档一共几十到几百 chunk，精确扫描最快最准，别开 ANN。**
3. **embedding 不可用时静默跳过向量**
   - `rebuild_indexes()`：FTS 一定做完；向量那步 `_VECTOR_OK=False` 直接跳过（[kb.py:L52-L58](file:///Users/lijia/Documents/02.Coder/企业级混合Agent工作流Demo/app/kb.py#L52-L58)）。
   - `scripts/index_kb.py`：`_VECTOR_OK=False` 直接 `sys.exit(1)`，给出"请安装 lancedb 依赖 / 启动 Ollama"提示。
     这就是你文档第 6 节"🔌（无 embedding/rerank 时自动降级关键词路）"的来源。
4. **维度防漂移** **`_check_dim`** **是保护锁**
   如果 OpenRouter 主用模型是 1024 维、但某个时候改环境变量切到了 768 维的模型，增量和全量都会**直接抛异常拒绝写入**——防止把两种维度的向量混在一张表里把查询全搞坏。修复方式：确认用哪个模型，然后全量重建一次。

***

## 四、一句话决策建议

> 95% 日常运维 = **管理 API 上传/删除（增量）**。只有「改分块/改 embedding 模型/批量导入≥50 篇/怀疑数据不一致」这 4 种情况才跑一次 **全量重建脚本**。

两条命令（几乎是你平时要记住的全部）：

```bash
# 日常：上传一篇文档（operator/admin token，增量，三条链路同步）
curl -X POST localhost:8000/api/kb/upload -F file=@x.pdf -H "X-Auth-Token: <token>"

# 特殊：改完 VECTOR_CHUNK_SIZE / EMBEDDING_MODEL 后（全量重建向量）
.venv/bin/python scripts/index_kb.py
```

<br />

<br />

<br />

<br />

<br />

<br />

<br />

<br />

<br />

