"""重排序（rerank）模块：OpenRouter /api/v1/rerank 主用 + Ollama /api/rerank 兜底。

- 主用：OpenRouter rerank 端点（默认 nvidia/llama-nemotron-rerank-vl-1b-v2:free），
  实测可用（POST {base}/rerank，返回 results[].relevance_score，按相关度降序）。
- 兜底：Ollama POST {base}/api/rerank（如 bce-reranker-base_v1）。
  关键事实：Ollama 主线至今（0.32.x）无原生 rerank 端点（404），且 rerank 模型
  走 /api/embed 会 GGML 崩溃——“能下载≠能完整用”。因此兜底采用「探测一次 +
  优雅降级」：进程内首次调用探测一次，可用则后续复用；不可用则返回 None，
  由调用方保持原召回顺序（== 不精排），绝不中断检索链路。
- 查询改写（query rewrite）也放本模块：用配置的 rewrite 模型把口语化提问改写成
  利于检索的关键词，默认关闭（涉及额外 LLM 调用）。
"""
from __future__ import annotations

from .config import settings
from .tracelog import log_exception, trace_call

# 进程级缓存：Ollama 兜底可用性（None=未探测，True/False=结果）
_ollama_rerank_ok: bool | None = None

# 精排单文档送检文本上限：只需判断相关度，不必带全文（省 token/上下文）
_RERANK_DOC_TRUNCATE = 1500
_RERANK_QUERY_TRUNCATE = 500


def _probe_ollama_rerank() -> bool:
    """探测 Ollama /api/rerank 是否可用；结果进程内缓存（首次探测一次）。"""
    global _ollama_rerank_ok
    if _ollama_rerank_ok is not None:
        return _ollama_rerank_ok
    try:
        import httpx
        url = (settings.rerank_fallback_base_url
               or "http://127.0.0.1:11434").rstrip("/") + "/api/rerank"
        r = httpx.post(url, json={"model": settings.rerank_fallback_model,
                                  "query": "ping", "documents": ["pong"]},
                       timeout=8)
        _ollama_rerank_ok = r.status_code == 200
    except Exception:
        log_exception()
        _ollama_rerank_ok = False
    return _ollama_rerank_ok

@trace_call("app.rerank.rerank")
def rerank(query: str, docs: list[dict], top_k: int) -> tuple[list[dict], str]:
    """对候选文档精排，返回 (排序后的 docs, 来源标识 source)。

    - docs 为 dict 列表（须含 "content"）。
    - 主用 OpenRouter /api/v1/rerank；失败落 Ollama 兜底；都不可用时返回
      (原序 docs, "skipped")，绝不中断检索链路。
    - 排序依据 relevance_score 降序；单文档截断到 _RERANK_DOC_TRUNCATE。
    """
    if not docs:
        return docs, "empty"
    texts = [d.get("content") or d.get("title") or "" for d in docs]
    texts = [t[:_RERANK_DOC_TRUNCATE] for t in texts]
    q = query[:_RERANK_QUERY_TRUNCATE]

    scored = _try_openrouter(q, texts)
    if scored is not None:
        return _apply(docs, scored, top_k), "openrouter"
    scored = _try_ollama(q, texts)
    if scored is not None:
        return _apply(docs, scored, top_k), "ollama"
    return docs, "skipped"


def _apply(docs: list[dict], scored: list[tuple[int, float]], top_k: int) -> list[dict]:
    """按 (index, relevance) 排序 docs，截取 top_k，并把精排分数写回 score 字段。"""
    order = sorted(scored, key=lambda t: -t[1])
    out: list[dict] = []
    for idx, s in order:
        if 0 <= idx < len(docs):
            item = dict(docs[idx])
            item["rerank_score"] = round(float(s), 4)
            out.append(item)
        if len(out) >= top_k:
            break
    return out


def _try_openrouter(query: str, texts: list[str]) -> list[tuple[int, float]] | None:
    """OpenRouter /api/v1/rerank；失败返回 None。"""
    if not (settings.openrouter_api_key or "").strip():
        return None
    try:
        import httpx
        url = (settings.rerank_base_url
               or "https://openrouter.ai/api/v1").rstrip("/") + "/rerank"
        payload = {
            "model": settings.rerank_model,
            "query": query,
            "documents": texts,
            "top_n": len(texts),
        }
        with httpx.Client(timeout=60) as client:
            r = client.post(url,
                            headers={"Authorization": f"Bearer {settings.openrouter_api_key}"},
                            json=payload)
            r.raise_for_status()
            data = r.json()
        return [(int(it["index"]), float(it["relevance_score"]))
                for it in data.get("results", [])]
    except Exception:
        log_exception()
        return None


def _try_ollama(query: str, texts: list[str]) -> list[tuple[int, float]] | None:
    """Ollama /api/rerank；探测到不可用则返回 None（缓存结果）。"""
    if not settings.rerank_fallback_model:
        return None
    if not _probe_ollama_rerank():
        return None
    try:
        import httpx
        url = (settings.rerank_fallback_base_url
               or "http://127.0.0.1:11434").rstrip("/") + "/api/rerank"
        payload = {"model": settings.rerank_fallback_model,
                   "query": query, "documents": texts}
        with httpx.Client(timeout=150) as client:
            r = client.post(url, json=payload)
            r.raise_for_status()
            data = r.json()
        # 兼容不同返回形态：results[] 或 rerank[]
        results = data.get("results") or data.get("rerank") or []
        return [(int(it["index"]), float(it.get("score", it.get("relevance_score", 0.0))))
                for it in results]
    except Exception:
        log_exception()
        return None

@trace_call("app.rerank.rewrite_query")
def rewrite_query(query: str) -> str:
    """LLM 查询改写：口语化 → 利于向量/关键词检索；失败/未启用时原样返回。

    仅当 settings.query_rewrite_enabled 开启时生效（默认关，零额外调用成本）。
    """
    if not settings.query_rewrite_enabled:
        return query
    try:
        from .llm_gateway import chat_with_fallback
        model = settings.rewrite_model or settings.classifier_model
        fb = settings.rewrite_fallback_model or settings.classifier_fallback_model
        system = (
            "你是企业 IT 知识库的查询改写器。把用户的提问改写为简短的关键词检索串："
            "保留核心实体（产品名、编号、服务名、专有名词），去掉口语冗余与语气词，"
            "中文优先，30 字以内。只输出改写后的检索词，不要任何解释。"
        )
        res = chat_with_fallback(
            [{"role": "system", "content": system},
             {"role": "user", "content": query}],
            primary_model=model, fallback_model=fb,
            max_tokens=settings.llm_max_tokens_rewrite,
        )
        new = (res.content or "").strip().strip('"').strip()
        if new and len(new) <= 60:
            return new
    except Exception:
        log_exception()
        pass
    return query


# ===== ② 多路召回：query 变体生成 =====


def _extract_keywords(query: str) -> list[str]:
    """规则抽取关键词（零 LLM 成本）：英文词/编号 + 中文 2-gram 实义片段。

    返回按"信息量"排序的去重候选（长词优先），供多路召回作独立变体。
    """
    import re as _re
    if not query:
        return []
    cands: list[str] = []
    # 英文词 / 编号 / 版本号（如 VPN、IT-SOP-001、7x24）
    for m in _re.finditer(r"[A-Za-z][A-Za-z0-9_.\-]{1,30}|[0-9]{1,6}[\u4e00-\u9fffA-Za-z]{0,6}", query):
        tok = m.group(0).strip()
        if tok and len(tok) >= 2 and tok not in cands:
            cands.append(tok)
    # 中文：整词（≤8 字）与 2-gram 混合，过滤纯语气词
    zh_words = _re.findall(r"[\u4e00-\u9fff]{2,8}", query)
    for w in zh_words:
        cands.append(w)
        if len(w) >= 3:
            for i in range(len(w) - 1):
                g = w[i:i + 2]
                if g not in ("怎么", "如何", "什么", "哪个", "没有", "还是", "是否", "一下"):
                    cands.append(g)
    # 去重保序，长词优先（信息量更大）
    seen, out = set(), []
    for c in sorted(cands, key=lambda s: -len(s)):
        if c not in seen:
            seen.add(c)
            out.append(c)
    return out[:6]


def _split_subquestions(query: str) -> list[str]:
    """LLM 把复合问句拆成 2~3 个子问题（额外 LLM 调用，仅多路+子问题开关开启时）。"""
    try:
        from .llm_gateway import chat_with_fallback
        model = settings.rewrite_model or settings.classifier_model
        fb = settings.rewrite_fallback_model or settings.classifier_fallback_model
        system = (
            "你是企业 IT 知识库的查询拆解器。若用户提问包含多个可独立检索的子问题，"
            "用分号分隔输出 2~3 个子问题；若只有一个问题则原样输出。只输出拆分结果。"
        )
        res = chat_with_fallback(
            [{"role": "system", "content": system},
             {"role": "user", "content": query}],
            primary_model=model, fallback_model=fb,
            max_tokens=settings.llm_max_tokens_rewrite,
        )
        parts = [p.strip() for p in (res.content or "").split(";") if p.strip()]
        return parts[:2] if len(parts) > 1 else []
    except Exception:
        log_exception()
        return []


def build_query_variants(query: str) -> list[str]:
    """② 多路召回：生成同一问题的多个可检索变体（原句/改写/关键词/子问题）。

    - 变体 1：原 query（必有）
    - 变体 2：LLM 改写（仅 query_rewrite_enabled 或 multi_query 开启时调用，失败跳过）
    - 变体 3：规则关键词串（零 LLM 成本，取 top1-2 个关键词拼成短语）
    - 变体 4：LLM 子问题拆分（仅 rag_multi_query_subsplit 开启）
    返回去重后 ≤ RAG_MULTI_QUERY_MAX_VARIANTS 个变体；任何 LLM 失败不影响至少有原句。
    """
    if not settings.rag_multi_query_enabled:
        return [query]
    variants: list[str] = [query]
    # 关键词变体：规则抽取，取前 1~2 个拼成短语（信息量最大优先）
    kws = _extract_keywords(query)
    if kws:
        variants.append(" ".join(kws[:2]) if any(c.isascii() for c in kws[:2]) else kws[0])
    # LLM 改写变体
    if settings.query_rewrite_enabled or settings.rag_multi_query_enabled:
        rw = rewrite_query(query)
        if rw and rw != query and rw not in variants:
            variants.append(rw)
    # 子问题拆分（可选，额外 LLM）
    if settings.rag_multi_query_subsplit:
        for sub in _split_subquestions(query):
            if sub and sub != query and sub not in variants:
                variants.append(sub)
                if len(variants) >= settings.rag_multi_query_max_variants:
                    break
    return variants[: max(1, settings.rag_multi_query_max_variants)]