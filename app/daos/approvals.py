"""审批 DAO：HITL 申请 / 通过 / 驳回。"""
import json

from ..db import session


def request(ticket_id, tool_name, params, requested_by) -> int:
    with session() as conn:
        cur = conn.execute(
            """INSERT INTO approvals (ticket_id, tool_name, params_json, requested_by)
               VALUES (?,?,?,?)""",
            (ticket_id, tool_name, json.dumps(params, ensure_ascii=False), requested_by))
        return cur.lastrowid


def decide(approval_id, status: str, decided_by, reason=""):
    with session() as conn:
        conn.execute(
            """UPDATE approvals SET status=?, decided_by=?, decided_at=datetime('now'), reason=?
               WHERE id=?""",
            (status, decided_by, reason, approval_id))


def get(approval_id):
    with session() as conn:
        return conn.execute("SELECT * FROM approvals WHERE id=?", (approval_id,)).fetchone()


def pending_for_ticket(ticket_id):
    with session() as conn:
        rows = conn.execute(
            "SELECT * FROM approvals WHERE ticket_id=? AND status='pending' ORDER BY id",
            (ticket_id,)).fetchall()
        return [dict(r) for r in rows]
