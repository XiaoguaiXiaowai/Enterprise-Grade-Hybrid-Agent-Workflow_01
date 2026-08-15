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
        _ollama_rerank_ok = False
    return _ollama_rerank_ok


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
        return None


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
        pass
    return query