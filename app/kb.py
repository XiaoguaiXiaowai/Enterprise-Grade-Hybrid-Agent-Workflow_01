"""知识库：SQLite FTS5 全文检索（零依赖 RAG 兜底方案，符合文档"SQLite FTS 兜底"）。"""
from .db import session

FTS_SCHEMA = """
CREATE VIRTUAL TABLE IF NOT EXISTS kb_fts USING fts5(title, content, tag);
"""


def init_kb() -> None:
    with session() as conn:
        conn.execute(FTS_SCHEMA)


def seed_kb(docs: list[dict]) -> None:
    with session() as conn:
        conn.execute("DELETE FROM kb_fts")
        for d in docs:
            conn.execute("INSERT INTO kb_fts (title, content, tag) VALUES (?,?,?)",
                         (d["title"], d["content"], d.get("tag", "it")))


def search(query: str, top_k: int = 3) -> list[dict]:
    with session() as conn:
        rows = conn.execute(
            "SELECT title, content, tag, bm25(kb_fts) AS rank FROM kb_fts WHERE kb_fts MATCH ? ORDER BY rank LIMIT ?",
            (query, top_k)).fetchall()
        return [dict(r) for r in rows]


def search_simple(query: str, top_k: int = 3) -> list[dict]:
    """FTS 不可用/无匹配时兜底：按空格分词，命中任意关键词即返回。"""
    # 中文无空格，拆分英文词与常见分隔；同时对整串做子串回退
    import re as _re
    words = [w for w in _re.split(r"[\s，。、;；,，]+", query) if w and len(w) >= 2]
    if not words:
        words = [query]
    conds = " OR ".join(["title LIKE ? OR content LIKE ?"] * len(words))
    params = []
    for w in words:
        params += [f"%{w}%", f"%{w}%"]
    with session() as conn:
        rows = conn.execute(
            f"SELECT title, content, tag FROM kb_fts WHERE {conds} LIMIT ?",
            (*params, top_k)).fetchall()
        return [dict(r) for r in rows]
