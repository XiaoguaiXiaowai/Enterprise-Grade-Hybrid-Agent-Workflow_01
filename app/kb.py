"""企业知识库：多路检索 RRF 融合 + LIKE 兜底，混合 RAG。

- 向量召回（Ollama bge-m3 + LanceDB）：解决同义/口语化查询命中问题。
- 关键词召回（SQLite FTS5 bm25）：保证术语/精确编号命中。
- RRF（倒数排名融合）：两条路同时跑，按"排名"而非原始分值合并打分，
  两路结果互为补充、互不吞没；向量不可用或没索引时自动退化为纯 FTS。
- search/search_simple 签名保持不变，上层工具无需改动。
"""
from .db import session
from . import vector_kb

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
    # 向量索引：尽力而为
    if vector_kb._VECTOR_OK:
        try:
            vec_docs = [{"title": d["title"], "tag": d["tag"], "content": d["content"]}
                        for d in docs]
            vector_kb.index_docs(vec_docs)
        except Exception:
            pass


def list_documents() -> list[dict]:
    with session() as conn:
        rows = conn.execute(
            "SELECT id, filename, title, content, tag, created_at FROM kb_documents ORDER BY id ASC"
        ).fetchall()
        return [dict(r) for r in rows]


def add_document(filename: str, title: str, content: str, tag: str = "doc") -> int:
    with session() as conn:
        cur = conn.execute(
            "INSERT INTO kb_documents (filename, title, content, tag) VALUES (?,?,?,?)",
            (filename, title, content, tag))
        new_id = cur.lastrowid
    rebuild_indexes()
    return new_id


def delete_document(doc_id: int) -> bool:
    with session() as conn:
        cur = conn.execute("DELETE FROM kb_documents WHERE id = ?", (doc_id,))
        if cur.rowcount == 0:
            return False
    rebuild_indexes()
    return True


def search(query: str, top_k: int = 3) -> list[dict]:
    """混合检索：向量 + 关键词（FTS5 主、LIKE 兜底）两路 RRF 融合。

    - 中文场景 FTS5 MATCH 常为空串，故关键词路对中文用 2-gram LIKE 兜底。
    - 向量不可用时退化为纯关键词路。
    """
    vec = _vector_hits(query, top_k)
    kw = _keyword_hits(query, top_k)
    if vec:
        return _rrf_merge(vec, kw, top_k)
    return kw


def _keyword_hits(query: str, top_k: int = 3) -> list[dict]:
    hits = _fts(query, top_k)
    if hits:
        return hits
    return _like(query, top_k)


def search_simple(query: str, top_k: int = 3) -> list[dict]:
    """FTS 不可用/无匹配时兜底：按空格分词，命中任意关键词即返回。"""
    if not _fts(query, top_k):
        return _like(query, top_k)
    return _fts(query, top_k)


def _vector_hits(query: str, top_k: int = 3) -> list[dict]:
    if not vector_kb.vector_enabled():
        return []
    try:
        return vector_kb.search(query, top_k)
    except Exception:
        return []


def _rrf_merge(vec: list[dict], fts: list[dict], top_k: int) -> list[dict]:
    """两路结果按文档标题去重，累加 1/(K+rank) 分数后排序返回。"""
    # 每篇文档: {"title", "tag", "content", "score", "_order"}（_order 保并列稳定，首见来源优先）
    by_title: dict[str, dict] = {}

    def add(rows: list[dict]) -> None:
        for rank, r in enumerate(rows, start=1):
            key = r["title"]
            entry = by_title.setdefault(key, {
                "title": key, "tag": r.get("tag", ""),
                "content": r["content"], "score": 0.0, "_order": len(by_title),
            })
            entry["score"] += 1.0 / (_RRF_K + rank)

    add(vec)
    add(fts)
    merged = sorted(by_title.values(), key=lambda e: (-e["score"], e["_order"]))
    for e in merged:
        e.pop("_order", None)
    return merged[:top_k]


def _fts(query: str, top_k: int = 3) -> list[dict]:
    try:
        with session() as conn:
            rows = conn.execute(
                "SELECT title, content, tag, bm25(kb_fts) AS rank FROM kb_fts WHERE kb_fts MATCH ? ORDER BY rank LIMIT ?",
                (query, top_k)).fetchall()
            return [dict(r) for r in rows]
    except Exception:
        return []


def _like(query: str, top_k: int = 3) -> list[dict]:
    """关键词兜底：按分隔拆词，任一词作为独立子串命中即返回（含中文整词）。"""
    import re as _re
    words = [w for w in _re.split(r"[\s，。、;；,，]+", query) if w]
    # 中文整句拆不出有意义的短词时，退化为对"名+动"等实义子串做匹配：
    # 做法：对每个>=2 的 2-gram 也发一次，扩大中文召回
    grams = []
    for w in words:
        if len(w) <= 1:
            grams.append(w)
        else:
            grams.append(w)
            for i in range(len(w) - 1):
                grams.append(w[i:i + 2])
    conds = " OR ".join(["title LIKE ? OR content LIKE ?"] * len(grams))
    params = []
    for g in grams:
        params += [f"%{g}%", f"%{g}%"]
    with session() as conn:
        rows = conn.execute(
            f"SELECT title, content, tag FROM kb_fts WHERE {conds} LIMIT ?",
            (*params, top_k)).fetchall()
        return [dict(r) for r in rows]
