"""向量知识库：Ollama bge-m3 生成 embedding，LanceDB 存向量做语义检索。

设计：
- embedding 走 OpenAI 兼容接口（Ollama /v1/embeddings），bge-m3 默认 1024 维，支持多语言（含中文）。
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


def _embedding_client():
    """惰性构造 OpenAI 兼容客户端，指向 Ollama embeddings 端点。"""
    from openai import OpenAI
    return OpenAI(base_url=settings.embedding_base_url, api_key="ollama")


def embed(texts: list[str]) -> list[list[float]]:
    """批量生成 embedding。失败抛异常，由调用方降级。"""
    client = _embedding_client()
    resp = client.embeddings.create(model=settings.embedding_model, input=texts)
    ordered = sorted(resp.data, key=lambda d: d.index)
    return [d.embedding for d in ordered]


def _chunk_text(text: str) -> list[str]:
    """按段落/句子切块，控制单块长度与重叠（防止语义切碎）。"""
    size, overlap = settings.vector_chunk_size, settings.vector_chunk_overlap
    # 先按空行/换行切段，再按字符长度合并
    raw = [seg.strip() for seg in re.split(r"\n+", text) if seg.strip()]
    here, chunks = [], []
    for seg in raw:
        # 超长单段：内部按长度硬切
        while len(seg) > size:
            piece, seg = seg[:size], seg[size:]
            chunks.append(piece)
        if here and len("".join(here)) + len(seg) > size:
            joined = "".join(here)
            chunks.append(joined[-overlap:] + seg if overlap else joined + seg)
            here = [seg] if not overlap else []
        else:
            here.append(seg)
    if here:
        chunks.append("".join(here))
    return chunks


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
