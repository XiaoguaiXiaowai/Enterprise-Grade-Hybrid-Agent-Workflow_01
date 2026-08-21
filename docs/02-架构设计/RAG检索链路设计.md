# RAG 检索链路设计

> 状态：现行 · 最后校验：2026-08-21 · 校验方式：符号级 + 行为实测（`scripts/eval_rag.py` 16 条基线 + `scripts/verify_rag_features.py` 8 项断言）
> 本页讲**为什么这么设计**（检索链路选型与权衡）；功能面（怎么用、配置、API）见 [02-RAG知识库.md](../01-核心功能/02-RAG知识库.md)。

---

## 1. 设计目标

企业知识库检索要同时满足四个指标，单路召回做不到：

| 指标 | 含义 | 单路向量 / 单路关键词的短板 |
|---|---|---|
| recall@1 | 首个命中是否相关 | 向量对术语/编号型查询弱；关键词对口语长句弱 |
| MRR | 命中排位 | 单路排序质量有限 |
| nDCG@3 | 前 3 相关性 | 同上 |
| 可用性 | 离线/降级可演示 | embedding/rerank 外部依赖会断链 |

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

| 环节 | 选型 | 为什么 | 降级策略 |
|---|---|---|---|
| 多路召回 | **多 query 变体并行**（默认关）：原句 + 规则关键词 + LLM 改写 + LLM 子问题，各变体独立走两路后统一 RRF 合并 | 单 query 的两路召回对"措辞差异大/多主题复合问句"覆盖面有限；变体间天然互补 | 规则关键词零成本必出；LLM 变体失败跳过；至少保留原句 |
| 向量召回 | LanceDB（本地文件，0 服务） | 单机零成本、无外部 DB 依赖 | 不可用 → 空召回，走纯关键词 |
| 关键词召回 | SQLite FTS5 + LIKE 兜底 | 与业务 DB 同源，术语/编号型查询强 | LIKE 永远可用 |
| 融合 | RRF（倒数排名融合） | 无需调参、对分数尺度不敏感 | 固定权重 60/40 → 自适应（开关） |
| 精排 | OpenRouter rerank → Ollama 探测 → 保持 RRF | 精排是 recall 提升最大的单一手段（实测 +6.2%） | 探测不可用 → `skipped` 保持 RRF 顺序，绝不中断链路 |
| 改写 | LLM 查询改写（默认关） | 口语化 → 检索友好关键词；有额外 LLM 成本 | 失败回落原文 |
| 上下文窗口 | 命中块 ±N 邻居拼接（`RAG_CONTEXT_WINDOW`，默认 1） | 分块保证检索精度，窗口补齐上下文完整度，两者解耦 | 单块文档无邻居时 context==content；读取失败降级无 context |
| 模糊澄清 | `search_kb` 返回 `needs_clarification` + 澄清问题；KnowledgeAgent 提示词要求输出问题而非硬答 | 无命中/低置信时硬答 = 幻觉来源；追问与既有 `POST /messages` 多轮链路天然衔接 | 开关关 → 退回"如实告知" |

**关键设计决策**：
1. **small-to-big 分块**：子块（500）做命中单位保证精确，父块（1500）做返回上下文保证完整；章节 `path` 元数据支持定位。
2. **上下文窗口与分块正交**：块内顺序号 `doc_idx` + 文档键 `doc_key` 落库（随 `upsert_docs`/`index_docs` 自动维护），命中后按 `doc_idx±N` 取同文档邻居拼接为 `context`；不改分块策略、不重训向量，纯检索侧增强，0 开关时代码路径与原链路一致。
3. **多路召回默认关**：与 query 改写同成本考量（规则关键词变体零成本、常驻；仅 LLM 改写/子问题动模型）；开启后任何 LLM 失败自动跳过该变体，至少保留原句——多路是"召回面"的加法，不是链路风险点。
4. **澄清是信号不是状态机**：不新增工单状态，`search_kb` 返回结构化信号 + 提示词约束，Agent 的输出结论即"提问"，用户回答走既有 followup 触发新一轮——与系统异步消息模型零冲突。
5. **增量维护**：`add_document`/`delete_document` 走向量 upsert/delete + FTS 行级同步，不整库重建（重建保留给 seed/脚本）。
6. **维度防漂移**：embedding 主/兜底模型维度不一致直接报错（`VECTOR_EMBED_DIM_CHECK`），防悄悄写坏索引。
7. **ANN 延迟开启**：小库精确扫描更快更准，`VECTOR_ANN_ENABLED=false` 默认；数据量上来再开 IVF_PQ/HNSW_SQ。
8. **评测先行**：16 条人工标注查询作为回归基线（`scripts/eval_rag.py --compare / --multi on`），`scripts/verify_rag_features.py` 对三块新能力做 8 项断言——**检索质量可量化，不靠感觉**。

## 3. 实测基线（16 条查询，2026-08-18 随播种数据对齐后重测）

| 指标 | rerank 关 | rerank 开 | 提升 |
|---|---|---|---|
| recall@1 | 0.938 | 1.000 | +6.2% |
| recall@3 | 1.000 | 1.000 | — |
| MRR | 0.969 | 1.000 | +3.1% |
| nDCG@3 | 0.977 | 1.000 | +2.3% |

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
