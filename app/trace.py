"""Trace：把每次模型调用/工具调用/状态变化写入 SQLite traces 表，支持全链路回放。"""
import json
import uuid
from datetime import datetime

from .db import session


def new_req_id() -> str:
    return uuid.uuid4().hex[:12]


def write(req_id, ticket_id, *, model="", provider="", prompt_version="", tool_name="",
         io=None, state_before="", state_after="", latency_ms=0, token_usage=None,
         retry_count=0, context_source=""):
    with session() as conn:
        conn.execute(
            """INSERT INTO traces
               (ticket_id, req_id, model, provider, prompt_version, context_source,
                tool_name, io_json, state_before, state_after, latency_ms,
                token_usage_json, retry_count)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (ticket_id, req_id, model, provider, prompt_version, context_source,
             tool_name, json.dumps(io or {}, ensure_ascii=False),
             state_before, state_after, latency_ms,
             json.dumps(token_usage or {}, ensure_ascii=False), retry_count),
        )


def list_for_ticket(ticket_id):
    with session() as conn:
        rows = conn.execute(
            "SELECT * FROM traces WHERE ticket_id = ? ORDER BY id ASC", (ticket_id,)).fetchall()
        return [dict(r) for r in rows]
