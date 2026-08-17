"""企业知识库：多路检索 RRF 融合 + LIKE 兜底 + rerank 精排，混合 RAG（整改⑧ 升级版）。

- 向量召回（Ollama bge-m3 + LanceDB）：解决同义/口语化查询命中问题。
- 关键词召回（SQLite FTS5 bm25）：保证术语/精确编号命中。
- 可选 LLM 查询改写（RAG_QUERY_REWRITE_ENABLED，默认关）：口语化 → 检索友好关键词。
- RRF（倒数排名融合）：两条路同时跑，按"排名"而非原始分值合并打分；
  支持自适应加权（RAG_ADAPTIVE_WEIGHT，默认关）按查询特征微调向量/关键词权重。
- rerank 精排（RERANK_ENABLED，默认开）：RRF 融合后由 OpenRouter rerank（或 Ollama 兜底）
  精排取 top_k；精排不可用自动降级为 RRF 顺序，不中断链路。
- 数据生命周期：add/delete 文档走增量维护（向量 upsert/delete + FTS 行级同步），
  不再全库重建向量索引（rebuild_indexes 仍保留给 seed/脚本全量用）。
- search/search_simple 签名保持兼容（新增可选的 tag 过滤参数），上层工具无需改动。
"""
from .config import settings
from .db import session
from . import vector_kb, rerank
from .tracelog import log_exception

FTS_SCHEMA = """
CREATE VIRTUAL TABLE IF NOT EXISTS kb_fts USING fts5(title, content, tag);
"""

# RRF 平滑常数：避免排第 1 的文档分数过大而压垮其余
_RRF_K = 60


def init_kb() -> None:
    with session() as conn:
        conn.execute(FTS_SCHEMA)


def seed_kb(docs: list[dict]) -> None:
    """以 kb_documents 为权威来源种入文档，并重建检索索引（FTS 必做、向量尽力）。"""
    with session() as conn:
        conn.execute("DELETE FROM kb_documents")
        for d in docs:
            conn.execute(
                "INSERT INTO kb_documents (filename, title, content, tag) VALUES (?,?,?,?)",
                (d.get("filename", d.get("title", "") + ".txt"),
                 d["title"], d["content"], d.get("tag", "doc")))
    rebuild_indexes()


def rebuild_indexes() -> None:
    """同步 FTS 表并尽力重建向量索引（Ollama 不可用/依赖缺失时静默跳过向量）。"""
    docs = list_documents()
    with session() as conn:
        conn.execute("DELETE FROM kb_fts")
        for d in docs:
            conn.execute("INSERT INTO kb_fts (title, content, tag) VALUES (?,?,?)",
                         (d["title"], d["content"], d["tag"]))
    if vector_kb._VECTOR_OK:
        try:
            vec_docs = [{"title": d["title"], "tag": d["tag"], "content": d["content"]}
                        for d in docs]
            vector_kb.index_docs(vec_docs)
        except Exception:
            log_exception()
            pass


def list_documents() -> list[dict]:
    with session() as conn:
        rows = conn.execute(
            "SELECT id, filename, title, content, tag, created_at FROM kb_documents ORDER BY id ASC"
        ).fetchall()
        return [dict(r) for r in rows]


def add_document(filename: str, title: str, content: str, tag: str = "doc") -> int:
    """新增文档：SQLite 落库 + FTS 增量 + 向量增量 upsert（整改④，不再全量重建）。"""
    with session() as conn:
        cur = conn.execute(
            "INSERT INTO kb_documents (filename, title, content, tag) VALUES (?,?,?,?)",
            (filename, title, content, tag))
        new_id = cur.lastrowid
    # FTS 增量：删旧同 title 再插入（保证内容更新幂等）
    with session() as conn:
        conn.execute("DELETE FROM kb_fts WHERE title = ?", (title,))
        conn.execute("INSERT INTO kb_fts (title, content, tag) VALUES (?,?,?)",
                     (title, content, tag))
    # 向量增量
    if vector_kb._VECTOR_OK:
        try:
            vector_kb.upsert_docs([{"title": title, "tag": tag, "content": content}])
        except Exception:
            log_exception()
            pass
    return new_id


def delete_document(doc_id: int) -> bool:
    """删除文档：SQLite + FTS + 向量增量删除。返回是否命中。"""
    with session() as conn:
        row = conn.execute("SELECT title FROM kb_documents WHERE id = ?", (doc_id,)).fetchone()
        if row is None:
            return False
        title = row["title"]
        conn.execute("DELETE FROM kb_documents WHERE id = ?", (doc_id,))
        conn.execute("DELETE FROM kb_fts WHERE title = ?", (title,))
    if vector_kb._VECTOR_OK:
        try:
            vector_kb.delete_docs([title])
        except Exception:
            log_exception()
            pass
    return True


def _resolve_top_k(top_k: int | None) -> int:
    """top_k 缺省引用配置 VECTOR_TOP_K。"""
    return settings.vector_top_k if top_k is None else top_k


def search(query: str, top_k: int | None = None, tag: str | None = None) -> list[dict]:
    """混合检索：查询改写(可选) → 向量 + 关键词两路召回 → RRF 融合 → rerank 精排。

    - 中文场景 FTS5 MATCH 常为空串，故关键词路对中文用 2-gram LIKE 兜底。
    - 向量不可用时退化为纯关键词路；rerank 不可用时保持融合顺序。
    - tag 非空时两路都按 tag 过滤（企业多业务线隔离）。
    """
    top_k = _resolve_top_k(top_k)
    # ② LLM 查询改写（默认关；开启后改写失败自动回落原文）
    q = rerank.rewrite_query(query)

    # rerank 开启时放大召回候选，精排后取 top_k
    recall_k = top_k * settings.rerank_recall_factor if settings.rerank_enabled else top_k

    vec = _vector_hits(q, recall_k, tag)
    kw = _keyword_hits(q, recall_k, tag)
    if vec:
        merged = _rrf_merge(q, vec, kw, recall_k)
    else:
        merged = kw or []
    if not merged:
        return []
    # ③ rerank 精排（OpenRouter 主 / Ollama 兜底 / 降级跳过）
    if settings.rerank_enabled and len(merged) > 1:
        merged, _ = rerank.rerank(q, merged, top_k)
    return merged[:top_k]


def _keyword_hits(query: str, top_k: int | None = None, tag: str | None = None) -> list[dict]:
    top_k = _resolve_top_k(top_k)
    hits = _fts(query, top_k, tag)
    if hits:
        return hits
    return _like(query, top_k, tag)


def search_simple(query: str, top_k: int | None = None) -> list[dict]:
    """FTS 不可用/无匹配时兜底：按空格分词，命中任意关键词即返回（内部审计用）。"""
    top_k = _resolve_top_k(top_k)
    hits = _fts(query, top_k)
    return hits if hits else _like(query, top_k)


def _vector_hits(query: str, top_k: int | None = None, tag: str | None = None) -> list[dict]:
    top_k = _resolve_top_k(top_k)
    if not vector_kb.vector_enabled():
        return []
    try:
        return vector_kb.search(query, top_k, tag=tag)
    except Exception:
        log_exception()
        return []


def _rrf_merge(query: str, vec: list[dict], fts: list[dict], top_k: int) -> list[dict]:
    """两路结果按文档标题去重，累加 1/(K+rank) 分数后排序返回。

    RAG_ADAPTIVE_WEIGHT 开启时按查询特征微调两路权重（术语/编号型 → 关键词加权；
    口语长句 → 向量加权）；默认等权重即原 RRF 行为。
    RAG_RETURN_PARENT 开启时用父块（small-to-big 大上下文）作为命中内容。
    """
    w_vec, w_kw = _adaptive_weights(query, fts_hit=bool(fts))
    by_title: dict[str, dict] = {}

    def add(rows: list[dict], weight: float) -> None:
        for rank, r in enumerate(rows, start=1):
            key = r["title"]
            body = r.get("parent") or r["content"] if settings.return_parent_chunk else r["content"]
            entry = by_title.setdefault(key, {
                "title": key, "tag": r.get("tag", ""),
                "content": body,
                "parent": r.get("parent", ""),
                "path": r.get("path", ""),
                "score": 0.0, "_order": len(by_title),
            })
            entry["score"] += weight / (_RRF_K + rank)

    add(vec, weight=w_vec)
    add(fts, weight=w_kw)
    merged = sorted(by_title.values(), key=lambda e: (-e["score"], e["_order"]))
    for e in merged:
        e.pop("_order", None)
    return merged[:top_k]


def _adaptive_weights(query: str, fts_hit: bool) -> tuple[float, float]:
    """按查询特征计算 (向量路权重, 关键词路权重)。默认 (1,1) 与原行为一致。"""
    if not settings.adaptive_rerank_weight or not query:
        return 1.0, 1.0
    import re as _re
    # 术语/编号特征：含英文词、版本号、括号、冒号等结构 → 关键词加权
    has_term = bool(_re.search(r"[A-Za-z]{2,}|\d{2,}|[（(][^）)]+[）)]|[:：]", query))
    if has_term:
        return 0.9, 1.2
    # 口语长句（>12 字且无术语）→ 向量加权
    return (1.2, 0.9) if len(query) > 12 else (1.0, 1.0)


def _fts(query: str, top_k: int | None = None, tag: str | None = None) -> list[dict]:
    top_k = _resolve_top_k(top_k)
    try:
        with session() as conn:
            sql = ("SELECT title, content, tag, bm25(kb_fts) AS rank FROM kb_fts "
                   "WHERE kb_fts MATCH ?")
            params: list = [query]
            if tag:
                sql += " AND tag = ?"
                params.append(tag)
            sql += " ORDER BY rank LIMIT ?"
            params.append(top_k)
            rows = conn.execute(sql, params).fetchall()
            return [dict(r) for r in rows]
    except Exception:
        log_exception()
        return []


def _like(query: str, top_k: int | None = None, tag: str | None = None) -> list[dict]:
    """关键词兜底：按分隔拆词，任一词作为独立子串命中即返回（含中文 2-gram）。"""
    top_k = _resolve_top_k(top_k)
    import re as _re
    words = [w for w in _re.split(r"[\s，。、;；,，]+", query) if w]
    grams = []
    for w in words:
        if len(w) <= 1:
            grams.append(w)
        else:
            grams.append(w)
            for i in range(len(w) - 1):
                grams.append(w[i:i + 2])
    conds = " OR ".join(["title LIKE ? OR content LIKE ?"] * len(grams))
    params: list = []
    for g in grams:
        params += [f"%{g}%", f"%{g}%"]
    with session() as conn:
        sql = f"SELECT title, tag, content FROM kb_fts WHERE ({conds})"
        if tag:
            sql += " AND tag = ?"
            params.append(tag)
        sql += " LIMIT ?"
        params.append(top_k)
        rows = conn.execute(sql, params).fetchall()
        return [dict(r) for r in rows]