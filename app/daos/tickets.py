"""工单 DAO：创建、查询、状态转移（带审计事件），单一写入口。"""
import json

from ..db import session
from ..state_machine import can_transition, InvalidTransition


def create_ticket(tenant_id, requester_id, title, description,
                  risk_level="low", intent_type="knowledge", priority="normal") -> int:
    with session() as conn:
        cur = conn.execute(
            """INSERT INTO tickets
               (tenant_id, requester_id, title, description, risk_level,
                intent_type, status, priority)
               VALUES (?,?,?,?,?,?, 'created', ?)""",
            (tenant_id, requester_id, title, description, risk_level, intent_type, priority),
        )
        return cur.lastrowid


def get_ticket(ticket_id):
    with session() as conn:
        return conn.execute("SELECT * FROM tickets WHERE id = ?", (ticket_id,)).fetchone()


def list_tickets(tenant_id, limit=50, requester_id=None):
    """列出工单。requester_id 非空时仅返回该用户创建/归属的个人工单。"""
    with session() as conn:
        if requester_id is not None:
            rows = conn.execute(
                "SELECT * FROM tickets WHERE tenant_id = ? AND requester_id = ? ORDER BY id DESC LIMIT ?",
                (tenant_id, requester_id, limit),
            ).fetchall()
        else:
            rows = conn.execute(
                "SELECT * FROM tickets WHERE tenant_id = ? ORDER BY id DESC LIMIT ?",
                (tenant_id, limit),
            ).fetchall()
        return [dict(r) for r in rows]


def _touch(conn, ticket_id):
    conn.execute("UPDATE tickets SET updated_at = datetime('now') WHERE id = ?", (ticket_id,))


def transition(ticket_id, to, actor, reason=""):
    """校验合法后修改状态，并写入 ticket_events 审计。返回新状态或抛 InvalidTransition。"""
    def _apply(conn):
        row = conn.execute("SELECT status FROM tickets WHERE id = ?", (ticket_id,)).fetchone()
        if not row:
            raise KeyError(f"ticket {ticket_id} not found")
        frm = row["status"]
        if not can_transition(frm, to):
            raise InvalidTransition(frm, to)
        conn.execute("UPDATE tickets SET status = ? WHERE id = ?", (to, ticket_id))
        _touch(conn, ticket_id)
        conn.execute(
            "INSERT INTO ticket_events (ticket_id, event_type, payload_json, actor) VALUES (?,?,?,?)",
            (ticket_id, "state_transition",
             json.dumps({"from": frm, "to": to, "reason": reason}),
             actor),
        )
        return to
    with session() as conn:
        return _apply(conn)


def add_event(ticket_id, event_type, payload, actor):
    with session() as conn:
        conn.execute(
            "INSERT INTO ticket_events (ticket_id, event_type, payload_json, actor) VALUES (?,?,?,?)",
            (ticket_id, event_type, json.dumps(payload, ensure_ascii=False), actor),
        )


def list_events(ticket_id):
    with session() as conn:
        rows = conn.execute(
            "SELECT * FROM ticket_events WHERE ticket_id = ? ORDER BY id ASC", (ticket_id,)
        ).fetchall()
        return [dict(r) for r in rows]


def list_events_after(ticket_id, after_id=0):
    """返回 id 大于 after_id 的新事件（用于 SSE 实时流式推送）。"""
    with session() as conn:
        rows = conn.execute(
            "SELECT * FROM ticket_events WHERE ticket_id = ? AND id > ? ORDER BY id ASC",
            (ticket_id, after_id),
        ).fetchall()
        return [dict(r) for r in rows]
