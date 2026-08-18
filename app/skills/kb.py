"""只读工具：企业知识库检索（SQLite FTS5 RAG + LIKE 兜底）。低风险。"""
from .registry import register_tool
from ..config import settings
from ..tracelog import log_exception, trace_call

try:
    from ..kb import search, search_simple
except Exception:
    log_exception()
    def search(q, top_k=None): return []
    def search_simple(q, top_k=None): return []

_FALLBACK = {"content": "未命中知识库，建议咨询对应负责人或补充工单描述。"}


@register_tool(name="search_kb", risk="low", description="检索企业知识库，返回匹配的 IT/运维文档片段（只读）。")
@trace_call("tool.kb.search_kb")
def search_kb(query: str, top_k: int | None = None) -> dict:
    # top_k 缺省引用配置 VECTOR_TOP_K（整改②）
    if top_k is None:
        top_k = settings.vector_top_k
    # 先尝试 FTS；无匹配再退 LIKE
    try:
        hits = search(query, top_k)
    except Exception:
        log_exception()
        hits = []
    if not hits:
        try:
            hits = search_simple(query, top_k)
        except Exception:
            log_exception()
            hits = []
    if not hits:
        hits = [{"title": "no_match", "content": _FALLBACK["content"], "rank": 0}]
    return {"hits": hits, "source": "hybrid_kb"}


@register_tool(name="search_kb_direct", risk="low",
               description="直接查看企业知识库某主题条目（内部审计用）。")
@trace_call("tool.kb.search_kb_direct")
def search_kb_direct(topic: str) -> dict:
    rows = search_simple(topic, 1) if _has_kb() else []
    return {"rows": rows, "source": "hybrid_kb"}


def _has_kb() -> bool:
    try:
        from ..db import session
        with session() as conn:
            return conn.execute("SELECT COUNT(*) c FROM kb_fts").fetchone()["c"] > 0
    except Exception:
        log_exception()
        return False
