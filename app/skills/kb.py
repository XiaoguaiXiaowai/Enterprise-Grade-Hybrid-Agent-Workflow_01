"""只读工具：企业知识库检索（SQLite FTS5 RAG + LIKE 兜底 + rerank 精排）。低风险。"""
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

# ① 模糊追问：低置信时的引导问题模板（提示用户补充关键信息）
_CLARIFY_QUESTIONS = [
    "请补充更具体的描述：涉及哪个系统/服务，报错现象是什么？",
    "请提供相关编号、账号或时间段等信息，方便精准定位。",
    "你已经尝试过哪些操作步骤？效果如何？",
]


def _confidence(hits: list[dict]) -> float | None:
    """取 top1 命中置信度：优先 rerank 分数，回退距离分转换；无分数返回 None。"""
    if not hits:
        return None
    h = hits[0]
    s = h.get("rerank_score")
    if s is None:
        s = h.get("score")
    if s is None:
        return None
    return float(s)


@register_tool(name="search_kb", risk="low", description="检索企业知识库，返回匹配的 IT/运维文档片段（只读）。")
@trace_call("tool.kb.search_kb")
def search_kb(query: str, top_k: int | None = None) -> dict:
    # top_k 缺省引用配置 VECTOR_TOP_K（整改②）
    if top_k is None:
        top_k = settings.vector_top_k
    # 混合检索（向量 + 关键词 + 可选多路 / rerank 精排）
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

    out = {"hits": hits, "source": "hybrid_kb"}
    # ① 模糊追问信号：无命中 或 top1 置信度低于阈值 → 提示 Agent 向用户澄清
    if settings.rag_clarify_enabled:
        conf = _confidence(hits)
        low_conf = hits[0].get("title") == "no_match" or (
            conf is not None and settings.rag_clarify_threshold > 0
            and conf < settings.rag_clarify_threshold)
        if low_conf:
            out["needs_clarification"] = True
            out["clarify_questions"] = _CLARIFY_QUESTIONS
    return out


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
