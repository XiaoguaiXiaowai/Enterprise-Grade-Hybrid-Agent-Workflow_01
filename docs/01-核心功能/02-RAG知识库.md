# RAG 知识库（双路召回 + RRF + rerank）

> 状态：现行 · 最后校验：2026-08-18 · 校验方式：符号级 + 行为实测（`scripts/eval_rag.py`）
> 一句话：企业知识库检索链路 = **查询改写(可选) → 双路召回 → RRF 融合 → rerank 精排**，并带完整的数据生命周期与评测基线。
> 代码入口见 [能力-代码映射表.md](06-能力-代码映射表.md) ⑨。

---

## 1. 检索流水线

```
query ──(可选 LLM 查询改写)──► 向量召回(LanceDB) ─┐
                                 关键词召回(FTS5+LIKE) ─┴─► RRF 融合 ──► rerank 精排 ──► top_k
```

| 环节 | 实现 | 关键函数 |
|---|---|---|
| 查询改写（可选） | `RAG_QUERY_REWRITE_ENABLED`（默认关）；口语化 → 检索友好关键词，失败回落原文 | `app/rerank.py:rewrite_query` |
| 向量召回 | LanceDB（`data/vector-kb`），主用 OpenRouter embedding ⇄ 兜底 Ollama bge-m3 | `app/vector_kb.py:search` / `embed` |
| 关键词召回 | SQLite FTS5 + LIKE 兜底 | `app/kb.py:_fts` / `_like` |
| RRF 融合 | 自适应权重（`RAG_ADAPTIVE_WEIGHT`，默认关：术语/编号型上调关键词权重、口语长句上调向量权重） | `app/kb.py:_rrf_merge` / `_adaptive_weights` |
| rerank 精排 | OpenRouter `/api/v1/rerank`（`nvidia/llama-nemotron-rerank-vl-1b-v2:free`）→ Ollama 探测降级 → 不可用保持 RRF 顺序（`skipped`），绝不中断链路 | `app/rerank.py:rerank` / `_probe_ollama_rerank` |
| 返回 | 召回候选先放大 `RERANK_RECALL_FACTOR`（默认 4）倍再精排取 top_k；`RAG_RETURN_PARENT` 控制是否携带父块 | `app/kb.py:search` |

## 2. 分块：small-to-big

- 子块 `VECTOR_CHUNK_SIZE=500`（检索命中单位）+ 父块 `VECTOR_PARENT_CHUNK_SIZE=1500`（命中时携带的大上下文；0 = 整篇作父块）；
- 从 `第X章 / X.X / # 标题` 样式行提取结构化 `path` 元数据（如 `目的 > 1 申请与业务审批`），支持按章节定位；
- 重叠 `VECTOR_CHUNK_OVERLAP=200` 防切句。

## 3. 索引与数据生命周期

| 操作 | 实现 |
|---|---|
| 增量维护 | `add_document`/`delete_document`（`app/kb.py`）→ 向量 upsert/delete + FTS 行级同步（`app/vector_kb.py:upsert_docs` / `delete_docs`）；不再整库重建 |
| 全量重建 | `rebuild_indexes` 保留给 seed（`scripts/index_kb.py` 显式重建） |
| tag 隔离 | `search(query, top_k, tag=...)` 向量路与关键词路都按 tag 过滤（企业多业务线隔离） |
| 维度防漂移 | `VECTOR_EMBED_DIM_CHECK=true`：主/兜底模型维度不一致直接报错，防写坏索引 |
| ANN 开关 | `VECTOR_ANN_ENABLED=false`（默认精确扫描更快更准；数据量大后开 `IVF_PQ` / `IVF_HNSW_SQ`） |

## 4. 工具面接入

- Agent 工具：`search_kb`（low）/ `search_kb_direct`（low），挂 KnowledgeAgent（`app/skills/kb.py`）；
- 管理 API（operator/admin）：`GET /api/kb` 列表、`POST /api/kb/upload`（PDF/Word/txt 解析）、`DELETE /api/kb/{doc_id}`（`app/api/kb.py`）；前端 `web/app/kb/page.tsx`；
- 演示播种：`scripts/seed_demo.py`（内置 IT-SOP-001 / IT-SEC-002 / IT-CHG-003 等文档）。

## 5. 评测基线

`scripts/eval_rag.py`：基于真实播种文档标注 **16 条查询**，量化 recall@1/3、MRR、nDCG@3：

```bash
python scripts/seed_demo.py                 # 先播种
python scripts/eval_rag.py --compare        # rerank on/off 对比
python scripts/eval_rag.py --detail         # 打印每条命中明细
```

**实测基线**（16 条查询，2026-08-18 随播种数据对齐后重测）：rerank 开启后 **recall@1 0.938 → 1.000（+6.2%）**，MRR 0.969 → 1.000，nDCG@3 0.977 → 1.000（recall@3 两档均为 1.000）。

## 6. 验证方式

| 验证项 | 命令 | 模式 |
|---|---|---|
| 评测基线 | `python scripts/eval_rag.py --compare` | 🔌（无 embedding/rerank 时自动降级关键词路） |
| 索引重建 | `python scripts/index_kb.py` | ✅ 需 embedding 可用 |
| 管理 API | `curl -X POST localhost:8000/api/kb/upload -F file=@x.pdf -H "X-Auth-Token: <token>"` | 🔌 |
| 检索工具 | 建一张「查 VPN 开通流程」低风险工单，观察 `search_kb` 调用与命中片段 | 🔌 |

## 7. 全部相关配置键

见 [配置清单.md](../03-部署运维/配置清单.md) 第 8/9 节（`EMBEDDING_*` / `VECTOR_*` / `RERANK_*` / `RAG_*` 共 23 键）。
