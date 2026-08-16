"""长期记忆 DAO：按 (tenant_id, key) 追加式写入，保留最近 N 条；读取取最新。

用途：工单收敛时把处理结论写入 memory，跨工单/跨轮次形成"长期记忆"，
上下文装配时经 <memo> 注入给 Agent（memory 表此前只有读没有写）。
"""
import json

from ..db import session

# 每个 key 最多保留的条目数（防止无限膨胀）
_MAX_ENTRIES = 10


def append(tenant_id: int, key: str, entry: dict, source: str = "agent") -> None:
    """追加一条记忆：读最新 value（JSON 数组）→ 追加 → 截断 → 写回。"""
    with session() as conn:
        row = conn.execute(
            "SELECT id, value_json FROM memory WHERE tenant_id=? AND key=? "
            "ORDER BY id DESC LIMIT 1", (tenant_id, key)).fetchone()
        entries: list = []
        if row:
            try:
                entries = json.loads(row["value_json"] or "[]")
            except Exception:
                entries = []
        if not isinstance(entries, list):
            entries = []
        entries.append(entry)
        entries = entries[-_MAX_ENTRIES:]
        value = json.dumps(entries, ensure_ascii=False)
        if row:
            conn.execute(
                "UPDATE memory SET value_json=?, source=?, updated_at=datetime('now') WHERE id=?",
                (value, source, row["id"]))
        else:
            conn.execute(
                "INSERT INTO memory (tenant_id, key, value_json, source) VALUES (?,?,?,?)",
                (tenant_id, key, value, source))


def latest(tenant_id: int, key: str) -> str | None:
    """取最新一条 value_json（未命中返回 None）。"""
    with session() as conn:
        row = conn.execute(
            "SELECT value_json FROM memory WHERE tenant_id=? AND key=? "
            "ORDER BY id DESC LIMIT 1", (tenant_id, key)).fetchone()
        return row["value_json"] if row else None
