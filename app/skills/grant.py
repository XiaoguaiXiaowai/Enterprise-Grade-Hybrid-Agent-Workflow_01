"""高风险工具：开通数据库只读账号（SQLite 持久化，演示后端）。

治理语义：
- 必须 HITL 审批 + 幂等（重复开通返回已存在，不重复插记录）
- 授权落 grants 表，进程重启后记录仍在，可真实回收（此前是进程内 dict，重启即丢）
- 默认 30 天自动过期（与知识库文案「默认 30 天回收」一致），过期记录惰性标记 expired
"""
from datetime import datetime, timedelta

from ..db import session
from .registry import register_tool
from .masker import mask_principal

# 授权有效期（天），可被环境变量覆盖（配置化，整改②风格）
GRANT_TTL_DAYS = 30


def _lazy_expire(conn) -> None:
    """把已过期的 active 授权惰性标记为 expired（查询时顺带清理）。"""
    conn.execute(
        "UPDATE grants SET status='expired' WHERE status='active' AND expires_at IS NOT NULL AND expires_at < ?",
        (datetime.now().isoformat(),))


def _active_grant(conn, principal: str, grant_type: str):
    _lazy_expire(conn)
    return conn.execute(
        "SELECT * FROM grants WHERE principal=? AND grant_type=? AND status='active' LIMIT 1",
        (principal, grant_type)).fetchone()


@register_tool(name="grant_db_readonly", risk="high", roles=("operator", "admin"),
               description="为指定用户开通数据库只读账号（高风险，需审批）。")
def grant_db_readonly(principal: str, grant_type: str = "readonly") -> dict:
    grant_type = grant_type.lower()
    if grant_type not in {"readonly", "readwrite"}:
        return {"ok": False, "error": f"非白名单授权类型: {grant_type}"}
    expires_at = (datetime.now() + timedelta(days=GRANT_TTL_DAYS)).isoformat()
    with session() as conn:
        existing = _active_grant(conn, principal, grant_type)
        if existing:
            return {"ok": True, "principal": mask_principal(principal),
                    "grant_type": grant_type, "role": existing["role"],
                    "expires_at": existing["expires_at"],
                    "note": "该授权已存在（幂等命中，未重复开通）"}
        conn.execute(
            "INSERT INTO grants (ticket_id, principal, grant_type, role, status, expires_at)"
            " VALUES (?,?,?,?, 'active', ?)",
            (None, principal, grant_type, "dba-reader", expires_at))
    return {"ok": True, "principal": mask_principal(principal),
            "grant_type": grant_type, "role": "dba-reader",
            "expires_at": expires_at,
            "note": f"已在演示系统开通（持久化），{GRANT_TTL_DAYS} 天后自动过期"}


@register_tool(name="revoke_db_readonly", risk="high", roles=("operator", "admin"),
               description="回收数据库只读权限（补偿动作，需审批）。")
def revoke_db_readonly(principal: str) -> dict:
    with session() as conn:
        _lazy_expire(conn)
        cur = conn.execute(
            "UPDATE grants SET status='revoked' WHERE principal=? AND status='active'",
            (principal,))
        existed = cur.rowcount > 0
    return {"ok": existed, "principal": mask_principal(principal),
            "note": "已回收权限" if existed else "无该授权记录"}
