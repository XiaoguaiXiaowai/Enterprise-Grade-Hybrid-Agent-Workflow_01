"""只读工具：企业知识库检索（SQLite FTS5 RAG + LIKE 兜底）。低风险。"""
from .registry import register_tool

try:
    from ..kb import search, search_simple
except Exception:
    def search(q, top_k=3): return []
    def search_simple(q, top_k=3): return []

_FALLBACK = {"content": "未命中知识库，建议咨询对应负责人或补充工单描述。"}


@register_tool(name="search_kb", risk="low", description="检索企业知识库，返回匹配的 IT/运维文档片段（只读）。")
def search_kb(query: str, top_k: int = 3) -> dict:
    # 先尝试 FTS；无匹配再退 LIKE
    try:
        hits = search(query, top_k)
    except Exception:
        hits = []
    if not hits:
        try:
            hits = search_simple(query, top_k)
        except Exception:
            hits = []
    if not hits:
        hits = [{"title": "no_match", "content": _FALLBACK["content"], "rank": 0}]
    return {"hits": hits, "source": "local_kb_fts"}


@register_tool(name="search_kb_direct", risk="low",
               description="直接查看企业知识库某主题条目（内部审计用）。")
def search_kb_direct(topic: str) -> dict:
    rows = search_simple(topic, 1) if _has_kb() else []
    return {"rows": rows, "source": "local_kb_fts"}


def _has_kb() -> bool:
    try:
        from ..db import session
        with session() as conn:
            return conn.execute("SELECT COUNT(*) c FROM kb_fts").fetchone()["c"] > 0
    except Exception:
        return False
