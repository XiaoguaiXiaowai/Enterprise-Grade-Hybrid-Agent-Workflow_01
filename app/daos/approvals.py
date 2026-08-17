"""审批 DAO：HITL 申请 / 通过 / 驳回 + RunState 恢复数据落库（P2-11）。"""
import json

from ..db import session


def request(ticket_id, tool_name, params, requested_by) -> int:
    with session() as conn:
        cur = conn.execute(
            """INSERT INTO approvals (ticket_id, tool_name, params_json, requested_by)
               VALUES (?,?,?,?)""",
            (ticket_id, tool_name, json.dumps(params, ensure_ascii=False), requested_by))
        return cur.lastrowid


def save_run_state(approval_id, *, run_state_json, routing_json,
                   run_ctx_json, budget_json, req_id, raw_item_json="") -> None:
    """保存 SDK RunState 序列化与恢复所需上下文（供进程重启后跨进程恢复）。

    raw_item_json：被审批工具调用的原始 raw_item（含真实 call_id/arguments），
    恢复时原样重建 ToolApprovalItem，保证 SDK 的审批决策指纹匹配。
    """
    with session() as conn:
        conn.execute(
            """UPDATE approvals SET run_state_json=?, routing_json=?, run_ctx_json=?,
               budget_json=?, req_id=?, raw_item_json=? WHERE id=?""",
            (run_state_json, routing_json, run_ctx_json, budget_json, req_id,
             raw_item_json, approval_id))


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


def list_approvals(tenant_id, limit=20, offset=0, statuses=None):
    """审批台分页一览：联表工单标题 + 租户隔离。

    - statuses: 状态集合（如 {"pending"} 或 {"approved","rejected"}），为空表示全部。
    按审批 id 倒序（最新在前），返回带 ticket_title 的审批字典列表。
    """
    where, params = ["a.ticket_id = t.id", "t.tenant_id = ?"], [tenant_id]
    if statuses:
        placeholders = ",".join("?" * len(statuses))
        where.append(f"a.status IN ({placeholders})")
        params.extend(list(statuses))
    sql = ("SELECT a.*, t.title AS ticket_title FROM approvals a "
           "JOIN tickets t ON t.id = a.ticket_id "
           f"WHERE {' AND '.join(where)} ORDER BY a.id DESC LIMIT ? OFFSET ?")
    params.extend([limit, offset])
    with session() as conn:
        rows = conn.execute(sql, params).fetchall()
        return [dict(r) for r in rows]


def count_approvals(tenant_id, statuses=None):
    """返回满足（租户 + 状态过滤）条件的审批总数（分页元数据用）。"""
    where, params = ["t.tenant_id = ?"], [tenant_id]
    if statuses:
        placeholders = ",".join("?" * len(statuses))
        where.append(f"a.status IN ({placeholders})")
        params.extend(list(statuses))
    with session() as conn:
        row = conn.execute(
            f"SELECT COUNT(*) AS n FROM approvals a JOIN tickets t ON t.id = a.ticket_id "
            f"WHERE {' AND '.join(where)}", params).fetchone()
        return row["n"]
