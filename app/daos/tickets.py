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


def list_tickets(tenant_id, limit=50, offset=0, requester_id=None, statuses=None, q=None):
    """列出工单（分页 + 状态/关键词过滤）。

    - requester_id 非空时仅返回该用户创建/归属的个人工单。
    - statuses: 状态集合（如 {"awaiting_approval"}），为空表示不过滤。
    - q: 搜索关键词（匹配工单 ID 数字或标题，忽略大小写）。
    联表带出创建人用户名（requester_name），供 operator/admin 全量视图区分归属。
    """
    where, params = [], [tenant_id]
    if requester_id is not None:
        where.append("t.requester_id = ?")
        params.append(requester_id)
    if statuses:
        placeholders = ",".join("?" * len(statuses))
        where.append(f"t.status IN ({placeholders})")
        params.extend(list(statuses))
    if q:
        like = f"%{q}%"
        where.append("(CAST(t.id AS TEXT) LIKE ? OR LOWER(t.title) LIKE LOWER(?))")
        params.extend([like, like])
    sql = ("SELECT t.*, u.username AS requester_name FROM tickets t "
           "LEFT JOIN users u ON u.id = t.requester_id WHERE t.tenant_id = ?")
    if where:
        sql += " AND " + " AND ".join(where)
    sql += " ORDER BY t.id DESC LIMIT ? OFFSET ?"
    params.extend([limit, offset])
    with session() as conn:
        rows = conn.execute(sql, params).fetchall()
        return [dict(r) for r in rows]


def count_tickets(tenant_id, requester_id=None, statuses=None, q=None):
    """返回满足（租户 + 可见范围 + 状态/关键词过滤）条件的工单总数（分页元数据用）。"""
    where, params = ["tenant_id = ?"], [tenant_id]
    if requester_id is not None:
        where.append("requester_id = ?")
        params.append(requester_id)
    if statuses:
        placeholders = ",".join("?" * len(statuses))
        where.append(f"status IN ({placeholders})")
        params.extend(list(statuses))
    if q:
        like = f"%{q}%"
        where.append("(CAST(id AS TEXT) LIKE ? OR LOWER(title) LIKE LOWER(?))")
        params.extend([like, like])
    with session() as conn:
        row = conn.execute(f"SELECT COUNT(*) AS n FROM tickets WHERE {' AND '.join(where)}",
                           params).fetchone()
        return row["n"]


def update_ticket_meta(ticket_id, risk_level=None, intent_type=None, priority=None):
    """更新工单风险/意图/优先级（快速落单后由分类阶段回写）。"""
    fields, params = [], []
    if risk_level is not None:
        fields.append("risk_level = ?"); params.append(risk_level)
    if intent_type is not None:
        fields.append("intent_type = ?"); params.append(intent_type)
    if priority is not None:
        fields.append("priority = ?"); params.append(priority)
    if not fields:
        return
    params.append(ticket_id)
    with session() as conn:
        conn.execute(f"UPDATE tickets SET {', '.join(fields)} WHERE id = ?", params)
        _touch(conn, ticket_id)


def delete_ticket(ticket_id) -> bool:
    """删除工单及其全部关联数据（事件/工具调用/审批/追踪/指标）。返回工单是否存在。"""
    with session() as conn:
        row = conn.execute("SELECT id FROM tickets WHERE id = ?", (ticket_id,)).fetchone()
        if row is None:
            return False
        for table in ("ticket_events", "tool_calls", "approvals", "traces", "metrics", "grants"):
            conn.execute(f"DELETE FROM {table} WHERE ticket_id = ?", (ticket_id,))
        conn.execute("DELETE FROM tickets WHERE id = ?", (ticket_id,))
        return True


def _touch(conn, ticket_id):
    conn.execute("UPDATE tickets SET updated_at = datetime('now') WHERE id = ?", (ticket_id,))


def transition(ticket_id, to, actor, reason=""):
    """校验合法后修改状态，并写入 ticket_events 审计。返回新状态或抛 InvalidTransition。

    并发防护（架构改善阶段1-2）：以"期望旧状态"为条件做 UPDATE，若行数=0
    说明状态已被其他请求并发修改，抛 InvalidTransition 阻止竞态双写。
    """
    def _apply(conn):
        row = conn.execute("SELECT status FROM tickets WHERE id = ?", (ticket_id,)).fetchone()
        if not row:
            raise KeyError(f"ticket {ticket_id} not found")
        frm = row["status"]
        if not can_transition(frm, to):
            raise InvalidTransition(frm, to)
        cur = conn.execute(
            "UPDATE tickets SET status = ?, updated_at = datetime('now') "
            "WHERE id = ? AND status = ?",
            (to, ticket_id, frm))
        if cur.rowcount != 1:
            # 并发修改：期望旧状态不匹配，阻止竞态双写
            raise InvalidTransition(frm, to)
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
