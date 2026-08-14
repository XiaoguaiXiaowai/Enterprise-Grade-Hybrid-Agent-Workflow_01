"""向量知识库：OpenRouter embedding 主用 + Ollama 兜底，LanceDB 存向量做语义检索。

设计：
- embedding 走 OpenAI 兼容接口：主用 OpenRouter（默认 nvidia/nemotron-3-embed-1b:free），
  失败时自动切换 Ollama 兜底（默认 bge-m3，1024 维，支持多语言含中文）。
- 向量库用 LanceDB 嵌入式模式，单目录落地在 data/vector-kb，无独立服务，贴合单机零成本。
- 依赖（lancedb/numpy/pyarrow）或 Ollama 不可用时优雅降级：返回空召回，由上层回退关键词检索。
"""
from __future__ import annotations

import re
from pathlib import Path

from .config import settings

# 仅在依赖可用时才启用向量能力
try:
    import lancedb
    import numpy as np
    _VECTOR_OK = True
except Exception:  # pragma: no cover - 依赖缺失时的降级
    lancedb = None
    np = None
    _VECTOR_OK = False


def vector_enabled() -> bool:
    """向量检索是否可用（依赖齐 + 已建索引）。"""
    if not _VECTOR_OK:
        return False
    return _is_indexed()


def _is_indexed() -> bool:
    try:
        db = _connect()
        return "kb_chunks" in db.table_names()
    except Exception:
        return False


def _connect():
    """打开（必要时创建）LanceDB。目录按 .env 的 VECTOR_DB_PATH 解析。"""
    p = Path(settings.vector_db_path)
    if not p.is_absolute():
        p = Path(__file__).resolve().parent.parent / p
    p.mkdir(parents=True, exist_ok=True)
    return lancedb.connect(str(p))


def _embedding_clients():
    """按配置构建两个 OpenAI 兼容客户端：主用(OpenRouter) 与 兜底(Ollama)。"""
    from openai import OpenAI
    return {
        "primary": OpenAI(base_url=settings.embedding_primary_base_url,
                          api_key=settings.openrouter_api_key or "sk-none"),
        "fallback": OpenAI(base_url=settings.embedding_base_url, api_key="ollama"),
    }


def embed(texts: list[str]) -> list[list[float]]:
    """按配置优先级生成 embedding：主用(OpenRouter) → 兜底(Ollama)。

    未配置有效主模型或主用调用失败时自动切换 Ollama 兜底；均失败抛异常，由调用方降级。
    """
    clients = _embedding_clients()

    # 1) 主用：仅在配置了有效 embedding 主模型时尝试（OpenRouter）
    primary_model = (settings.embedding_model or "").strip()
    if primary_model:
        try:
            resp = clients["primary"].embeddings.create(
                model=primary_model, input=texts)
            ordered = sorted(resp.data, key=lambda d: d.index)
            vecs = [d.embedding for d in ordered]
            if vecs and all(v for v in vecs):
                return vecs
        except Exception:
            pass  # 主用失败 → 落兜底

    # 2) 兜底：Ollama 本地 embedding
    fallback_model = (settings.embedding_fallback_model or "").strip()
    if fallback_model:
        resp = clients["fallback"].embeddings.create(
            model=fallback_model, input=texts)
        ordered = sorted(resp.data, key=lambda d: d.index)
        return [d.embedding for d in ordered]

    raise RuntimeError("未配置有效的 embedding 模型（EMBEDDING_MODEL / EMBEDDING_FALLBACK_MODEL）")


# 递归切分分隔符（从粗到细），零依赖复刻 langchain RecursiveCharacterTextSplitter 思路
_SEPARATORS = ["\n\n", "\n", "。", "！", "？", "；", "，", " ", ""]


def _split_by(text: str, sep: str) -> list[str]:
    """按单个分隔符切分，保留分隔符粘在前一段末尾（避免破坏句子结尾）。"""
    if sep == "":
        return list(text)
    parts = text.split(sep)
    out = []
    for i, part in enumerate(parts):
        piece = part
        if i < len(parts) - 1:
            piece = part + sep
        if piece:
            out.append(piece)
    return out


def _merge_chunks(segments: list[str], size: int, overlap: int) -> list[str]:
    """贪心把段合并到 size 以内；封块时带上上一块尾部 overlap 字符衔接。"""
    chunks: list[str] = []
    current = ""
    for seg in segments:
        if not current:
            current = seg
            continue
        if len(current) + len(seg) <= size:
            current += seg
        else:
            chunks.append(current)
            current = (current[-overlap:] if overlap else "") + seg
    if current:
        chunks.append(current)
    return chunks


def _chunk_text(text: str) -> list[str]:
    """递归切分：按段落→句子→子句→字符逐层降级，尽量在自然边界切断。

    与原 langchain 方案一致：先用最粗分隔符（\n\n）切，若某块仍超 size 就换下一级
    分隔符（\n、句末标点、逗号、空格）继续递归切，最后降级到按字符硬切。
    """
    size, overlap = settings.vector_chunk_size, settings.vector_chunk_overlap

    def recurse(t: str, idx: int = 0) -> list[str]:
        sep = _SEPARATORS[idx]
        if sep == "":
            # 终极降级：按字符拆成单字符片段
            return [ch for ch in t if ch != ""]
        pieces = _split_by(t, sep)
        out: list[str] = []
        for piece in pieces:
            if len(piece) <= size:
                out.append(piece)
            else:
                out.extend(recurse(piece, idx + 1))
        return out

    # 仅做一次最终合并（含 overlap），避免二次合并产生块长漂移
    return _merge_chunks(recurse(text), size, overlap)


def _normalize(vec: list[float]) -> list[float]:
    arr = np.asarray(vec, dtype="float32")
    norm = np.linalg.norm(arr)
    if norm == 0:
        return vec
    return (arr / norm).tolist()


def index_docs(docs: list[dict]) -> int:
    """切块 + embedding 写入 LanceDB（幂等：先清空该库再写）。返回写入 chunk 数。"""
    if not _VECTOR_OK:
        return 0
    ctx = _connect()
    table_name = "kb_chunks"
    all_rows: list[dict] = []
    for d in docs:
        chunks = _chunk_text(d.get("content", ""))
        if not chunks:
            continue
        for chunk in chunks:
            all_rows.append({
                "id": f"{d.get('title','')}::{len(all_rows)}",
                "title": d.get("title", ""),
                "tag": d.get("tag", ""),
                "content": chunk,
            })
    if not all_rows:
        return 0
    texts = [r["content"] for r in all_rows]
    vectors = embed(texts)
    for row, vec in zip(all_rows, vectors):
        row["vector"] = _normalize(vec)
    records = [
        {"id": r["id"], "title": r["title"], "tag": r["tag"],
         "content": r["content"], "vector": r["vector"]}
        for r in all_rows
    ]
    tbl = ctx.create_table(table_name, data=records, mode="overwrite")
    return tbl.count_rows()


def search(query: str, top_k: int = 3) -> list[dict]:
    """语义检索：对 query embedding 后做近似最近邻（余弦）。不可用/异常返回空列表。"""
    if not _VECTOR_OK or not _is_indexed():
        return []
    try:
        ctx = _connect()
        tbl = ctx.open_table("kb_chunks")
        qv = _normalize(embed([query])[0])
        hits = tbl.search(qv).limit(top_k).to_list()
        return [{
            "title": h["title"],
            "tag": h.get("tag", ""),
            "content": h["content"],
            "score": round(float(h.get("_distance", 0.0)), 4),
        } for h in hits]
    except Exception:
        return []
