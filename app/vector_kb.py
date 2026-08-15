"""向量知识库：OpenRouter embedding 主用 + Ollama 兜底，LanceDB 存向量做语义检索。

设计（整改⑧ 升级版）：
- embedding 走 OpenAI 兼容接口：主用 OpenRouter（默认 nvidia/nemotron-3-embed-1b:free），
  失败时自动切换 Ollama 兜底（默认 bge-m3，1024 维，支持多语言含中文）。
- 分块 small-to-big：子块（VECTOR_CHUNK_SIZE，用于检索命中）+ 父块（VECTOR_PARENT_CHUNK_SIZE，
  检索返回时携带更大上下文）；并从 "第X章 / X.X / #" 样式标题行提取结构化 path 元数据。
- 数据生命周期（整改④）：
  · index_docs 幂等全量重建（seed/脚本入口）；
  · upsert_docs/delete_docs 增量维护（kb.add_document/delete_document 走增量，不再全库重建）；
  · 写入时校验 embedding 维度（VECTOR_EMBED_DIM_CHECK），维度漂移直接报错而不是静默写坏索引；
  · search 支持按 tag 元数据过滤、可配 ANN 近似索引（VECTOR_ANN_ENABLED）。
- 依赖（lancedb/numpy/pyarrow）或 Ollama 不可用时优雅降级：返回空召回，由上层回退关键词检索。
"""
from __future__ import annotations

import logging
import re
from pathlib import Path

from .config import settings

logger = logging.getLogger(__name__)

# 仅在依赖可用时才启用向量能力
try:
    import lancedb
    import numpy as np
    _VECTOR_OK = True
except Exception:  # pragma: no cover - 依赖缺失时的降级
    lancedb = None
    np = None
    _VECTOR_OK = False

_TABLE = "kb_chunks"


def vector_enabled() -> bool:
    """向量检索是否可用（依赖齐 + 已建索引）。"""
    if not _VECTOR_OK:
        return False
    return _is_indexed()


def _is_indexed() -> bool:
    try:
        db = _connect()
        return _TABLE in db.table_names()
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
    from openai import OpenAI
    return {
        "primary": OpenAI(base_url=settings.embedding_primary_base_url,
                          api_key=settings.openrouter_api_key or "sk-none"),
        "fallback": OpenAI(base_url=settings.embedding_base_url, api_key="ollama"),
    }


def embed(texts: list[str]) -> list[list[float]]:
    """按配置优先级生成 embedding：主用(OpenRouter) → 兜底(Ollama)。"""
    clients = _embedding_clients()
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
    fallback_model = (settings.embedding_fallback_model or "").strip()
    if fallback_model:
        resp = clients["fallback"].embeddings.create(
            model=fallback_model, input=texts)
        ordered = sorted(resp.data, key=lambda d: d.index)
        return [d.embedding for d in ordered]
    raise RuntimeError("未配置有效的 embedding 模型（EMBEDDING_MODEL / EMBEDDING_FALLBACK_MODEL）")


# ===== 切分：子块（检索粒度） =====
_SEPARATORS = ["\n\n", "\n", "。", "！", "？", "；", "，", " ", ""]


def _split_by(text: str, sep: str) -> list[str]:
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


def _chunk_text(text: str, size: int | None = None, overlap: int | None = None) -> list[str]:
    """递归切分：段落→句子→子句→字符逐层降级（同 langchain 思路）。"""
    size = size if size is not None else settings.vector_chunk_size
    overlap = overlap if overlap is not None else settings.vector_chunk_overlap

    def recurse(t: str, idx: int = 0) -> list[str]:
        sep = _SEPARATORS[idx]
        if sep == "":
            return [ch for ch in t if ch != ""]
        pieces = _split_by(t, sep)
        out: list[str] = []
        for piece in pieces:
            if len(piece) <= size:
                out.append(piece)
            else:
                out.extend(recurse(piece, idx + 1))
        return out

    return _merge_chunks(recurse(text), size, overlap)


def _parent_blocks(text: str) -> list[str]:
    """父块（small-to-big 的大上下文）：按 VECTOR_PARENT_CHUNK_SIZE 且带重叠切；
    尺寸 <=0 时整篇作为单个父块。"""
    size = settings.vector_parent_chunk_size
    if size <= 0 or len(text) <= size:
        return [text]
    overlap = max(100, size // 5)
    return _chunk_text(text, size=size, overlap=overlap)


# ===== 结构化 path 元数据 =====
_HEAD_RE = re.compile(r"^\s{0,3}(#{1,3}\s+|第[一二三四五六七八九十百千]+[章节篇]\s*|\d{1,2}(?:\.\d{1,2})*\s*[、.．]\s*)")


def _heading_paths(text: str) -> list[tuple[int, str]]:
    """扫描标题行，返回 [(起始字符偏移, 分层路径)]；无标题返回 []。

    仅把明显标题行纳入路径（行长 ≤48 且匹配标题样式），逐级覆盖形成 "章节/小节" 树。
    """
    tree: list[str] = []
    out: list[tuple[int, str]] = []
    offset = 0
    for line in text.splitlines(keepends=True):
        raw = line.strip()
        m = _HEAD_RE.match(raw)
        if m and len(raw) <= 48:
            # 按 # 数量 / 数字层级调整树深度
            if raw.startswith("#"):
                depth = len(raw) - len(raw.lstrip("#"))
                glyph = raw.lstrip("#").strip()
            else:
                cnt = raw.split(" ", 1)[0].count(".") if " " in raw else 0
                depth = min(3, 1 + cnt)
                glyph = raw[m.end():].strip() or raw
            tree = tree[: max(0, depth - 1)] + [glyph]
        offset += len(line)
        if tree:
            out.append((offset, " > ".join(tree)))
    return out


def _chunk_path(chunk_start: int, paths: list[tuple[int, str]]) -> str:
    """取块起始偏移之前最近一次出现的标题路径。"""
    best = ""
    for off, p in paths:
        if off <= chunk_start:
            best = p
        else:
            break
    return best


# ===== 归一化 / 维度校验 =====
def _normalize(vec: list[float]) -> list[float]:
    arr = np.asarray(vec, dtype="float32")
    norm = np.linalg.norm(arr)
    if norm == 0:
        return vec
    return (arr / norm).tolist()


def _check_dim(vecs: list[list[float]], where: str) -> None:
    """维度校验：配置开启且与期望维度不一致时报错（防 embedding 模型切换写坏索引）。"""
    if not settings.vector_embed_dim_check:
        return
    dims = {len(v) for v in vecs}
    if len(dims) > 1 or (dims and settings.embedding_dim > 0 and dims != {settings.embedding_dim}):
        raise ValueError(
            f"embedding 维度漂移（{where}）：实际 {dims}，配置 EMBEDDING_DIM={settings.embedding_dim}。"
            f"请确认主/兜底模型输出维度一致，或同步修改 EMBEDDING_DIM。"
        )


def _build_rows(docs: list[dict], start_idx: int = 0) -> list[dict]:
    """把文档切块并构建记录（不 embedding，供调用方批量扩维后写入）。

    small-to-big：内容存子块，另存 parent 父块文本与 path 结构路径。
    """
    rows: list[dict] = []
    idx = start_idx
    for d in docs:
        content = d.get("content", "") or ""
        if not content.strip():
            continue
        children = _chunk_text(content)
        parents = _parent_blocks(content)
        paths = _heading_paths(content)
        if not children:
            continue
        # 子块 → 父块归属：按字符偏移贪心匹配（父块按顺序累计）
        for child in children:
            idx += 1
            # 找第一个覆盖该子块起始位置的父块（用子块在全文中的近似偏移）
            off = content.find(child[:30])
            parent = _pick_parent(parents, content, child, off)
            rows.append({
                "id": f"{d.get('title','')}::{idx}",
                "title": d.get("title", ""),
                "tag": d.get("tag", "doc"),
                "content": child,
                "parent": parent,
                "path": _chunk_path(off if off >= 0 else 0, paths),
            })
    return rows


def _pick_parent(parents: list[str], full: str, child: str, off: int) -> str:
    """把子块归入其所在父块（按字符位置近似匹配）。"""
    if len(parents) <= 1:
        return parents[0] if parents else ""
    if off < 0:
        return parents[0]
    acc = 0
    for p in parents:
        acc += len(p)
        if off < acc:
            return p
    return parents[-1]


def _maybe_create_ann_index(tbl) -> None:
    """按配置建 ANN 索引（VECTOR_ANN_ENABLED）。小库跳过并提示（精确扫描更快更准）。"""
    if not settings.vector_ann_enabled:
        return
    try:
        n = tbl.count_rows()
        if n < 1000:
            logger.info("向量库仅 %d 行，ANN 收益小，跳过 create_index（精确扫描更优）", n)
            return
        tbl.create_index(
            metric=settings.vector_ann_metric,
            vector_column_name="vector",
            index_type=settings.vector_ann_index_type,
            replace=True,
        )
        logger.info("已创建 ANN 索引: %s", settings.vector_ann_index_type)
    except Exception as e:  # noqa: BLE001
        logger.warning("ANN 索引创建失败，继续用精确扫描: %s", e)


def index_docs(docs: list[dict]) -> int:
    """全量重建向量索引（幂等：先清空该库再写）。返回写入 chunk 数。"""
    if not _VECTOR_OK:
        return 0
    rows = _build_rows(docs)
    if not rows:
        return 0
    texts = [r["content"] for r in rows]
    vectors = embed(texts)
    _check_dim(vectors, "index_docs")
    for row, vec in zip(rows, vectors):
        row["vector"] = _normalize(vec)
    ctx = _connect()
    tbl = ctx.create_table(_TABLE, data=rows, mode="overwrite")
    _maybe_create_ann_index(tbl)
    return tbl.count_rows()


def upsert_docs(docs: list[dict]) -> int:
    """增量 upsert（整改④）：按 title 删除旧 chunk 写入新 chunk，不动其他文档。

    用于 kb.add_document：避免每次增删都全库重建 + 重训向量。返回本次写入 chunk 数。
    """
    if not _VECTOR_OK or not _is_indexed():
        return 0 if not _VECTOR_OK else index_docs(docs)
    rows = _build_rows(docs)
    if not rows:
        return 0
    texts = [r["content"] for r in rows]
    vectors = embed(texts)
    _check_dim(vectors, "upsert_docs")
    for row, vec in zip(rows, vectors):
        row["vector"] = _normalize(vec)
    ctx = _connect()
    tbl = ctx.open_table(_TABLE)
    titles = {r["title"] for r in rows}
    for t in titles:
        try:
            tbl.delete(f"title = '{_sql_escape(t)}'")
        except Exception:
            pass  # 无该 title 也 OK
    tbl.add(rows)
    return len(rows)


def delete_docs(doc_titles: list[str]) -> int:
    """按标题删除向量 chunk（整改④）。返回删除行数。"""
    if not _VECTOR_OK or not _is_indexed():
        return 0
    ctx = _connect()
    tbl = ctx.open_table(_TABLE)
    total = 0
    for t in doc_titles:
        try:
            total += tbl.delete(f"title = '{_sql_escape(t)}'")
        except Exception:
            pass
    return total


def _sql_escape(s: str) -> str:
    return s.replace("'", "''")


def search(query: str, top_k: int | None = None, tag: str | None = None) -> list[dict]:
    """语义检索：query embedding → 近似最近邻（余弦，向量已归一化）。

    - tag 非空时按 tag 元数据过滤（LB 的 where 预过滤）。
    - 返回元素含 title/tag/content/parent/path/score。
    - 不可用/异常返回空列表，由上层回退关键词检索。
    """
    if top_k is None:
        top_k = settings.vector_top_k
    if not _VECTOR_OK or not _is_indexed():
        return []
    try:
        ctx = _connect()
        tbl = ctx.open_table(_TABLE)
        qv = _normalize(embed([query])[0])
        q = tbl.search(qv).limit(top_k)
        if tag:
            q = q.where(f"tag = '{_sql_escape(tag)}'", prefilter=True)
        hits = q.to_list()
        return [{
            "title": h["title"],
            "tag": h.get("tag", ""),
            "content": h["content"],
            "parent": h.get("parent", ""),
            "path": h.get("path", ""),
            "score": round(float(h.get("_distance", 0.0)), 4),
        } for h in hits]
    except Exception as e:  # noqa: BLE001
        logger.warning("向量检索异常，回退关键词: %s", e)
        return []