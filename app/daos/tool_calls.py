"""工具调用 DAO：记录调用、结果与幂等。"""
import json

from ..db import session
from ..guards import fingerprint


def record(ticket_id, tool_name, params, result, status, idempotency_key,
           latency_ms, retry_count=0):
    with session() as conn:
        conn.execute(
            """INSERT INTO tool_calls
               (ticket_id, tool_name, params_json, result_json, status,
                idempotency_key, latency_ms, retry_count)
               VALUES (?,?,?,?,?,?,?,?)""",
            (ticket_id, tool_name, json.dumps(params, ensure_ascii=False),
             json.dumps(result, ensure_ascii=False), status,
             idempotency_key, latency_ms, retry_count))


def latest_success(ticket_id, tool_name, params):
    key = fingerprint(f"ticket:{ticket_id}", tool_name, params)
    with session() as conn:
        row = conn.execute(
            "SELECT * FROM tool_calls WHERE ticket_id=? AND idempotency_key=? AND status='done' ORDER BY id DESC LIMIT 1",
            (ticket_id, key)).fetchone()
        return dict(row) if row else None
