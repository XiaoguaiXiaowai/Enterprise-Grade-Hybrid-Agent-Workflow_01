# RAG 检索链路设计

> 状态：现行 · 最后校验：2026-08-18 · 校验方式：符号级 + 行为实测（`scripts/eval_rag.py` 16 条基线）
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

## 2. 链路选型与权衡

```
query ──(可选改写)──► 向量召回(LanceDB) ─┐
                       关键词召回(FTS5+LIKE) ─┴─► RRF 融合 ──► rerank 精排 ──► top_k
```

| 环节 | 选型 | 为什么 | 降级策略 |
|---|---|---|---|
| 向量召回 | LanceDB（本地文件，0 服务） | 单机零成本、无外部 DB 依赖 | 不可用 → 空召回，走纯关键词 |
| 关键词召回 | SQLite FTS5 + LIKE 兜底 | 与业务 DB 同源，术语/编号型查询强 | LIKE 永远可用 |
| 融合 | RRF（倒数排名融合） | 无需调参、对分数尺度不敏感 | 固定权重 60/40 → 自适应（开关） |
| 精排 | OpenRouter rerank → Ollama 探测 → 保持 RRF | 精排是 recall 提升最大的单一手段（实测 +6.2%） | 探测不可用 → `skipped` 保持 RRF 顺序，绝不中断链路 |
| 改写 | LLM 查询改写（默认关） | 口语化 → 检索友好关键词；有额外 LLM 成本 | 失败回落原文 |

**关键设计决策**：
1. **small-to-big 分块**：子块（500）做命中单位保证精确，父块（1500）做返回上下文保证完整；章节 `path` 元数据支持定位。
2. **增量维护**：`add_document`/`delete_document` 走向量 upsert/delete + FTS 行级同步，不整库重建（重建保留给 seed/脚本）。
3. **维度防漂移**：embedding 主/兜底模型维度不一致直接报错（`VECTOR_EMBED_DIM_CHECK`），防悄悄写坏索引。
4. **ANN 延迟开启**：小库精确扫描更快更准，`VECTOR_ANN_ENABLED=false` 默认；数据量上来再开 IVF_PQ/HNSW_SQ。
5. **评测先行**：16 条人工标注查询作为回归基线（`scripts/eval_rag.py`），rerank on/off 可对比——**检索质量可量化，不靠感觉**。

## 3. 实测基线（16 条查询，2026-08-18 随播种数据对齐后重测）

| 指标 | rerank 关 | rerank 开 | 提升 |
|---|---|---|---|
| recall@1 | 0.938 | 1.000 | +6.2% |
| recall@3 | 1.000 | 1.000 | — |
| MRR | 0.969 | 1.000 | +3.1% |
| nDCG@3 | 0.977 | 1.000 | +2.3% |

> 复现：`python scripts/seed_demo.py && python scripts/eval_rag.py --compare`（需 embedding/rerank 可用；离线自动降级关键词路）。评测集与 `scripts/seed_demo.py` 的 5 篇种子文档对齐（2026-08-18 曾因旧评测集引用已下线文档导致全 0，已修复）。

## 4. 与 Agent 的衔接

- `search_kb`/`search_kb_direct`（low，只读）挂 KnowledgeAgent——工具面见 [01-企业级Agent-Harness.md](../01-核心功能/01-企业级Agent-Harness.md)；
- 检索上下文进入「上下文装配」的选/压/截流程（`app/context_assembler.py`）；
- 知识库管理 API（upload/delete/list）面向 operator/admin，前端 `web/app/kb/page.tsx`。

## 5. 边界与后续方向

- 当前 embedding 主用依赖 OpenRouter（`.env.example` 空值陷阱见 [配置清单.md](../03-部署运维/配置清单.md) 陷阱 1）；完全离线需本地 Ollama bge-m3；
- 后续可选：混合检索权重在线学习、rerank 模型替换、知识库权限分级（tag 已支持业务线隔离）。
