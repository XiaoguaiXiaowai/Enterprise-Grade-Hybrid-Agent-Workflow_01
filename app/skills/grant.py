"""高风险工具：开通数据库只读账号（演示道具）。必须 HITL 审批 + 幂等。"""
from .registry import register_tool
from .masker import mask_principal

_GRANTS = {}  # principal -> {grant_type, role}，演示内存态


@register_tool(name="grant_db_readonly", risk="high", roles=("operator", "admin"),
               description="为指定用户开通数据库只读账号（高风险，需审批）。")
def grant_db_readonly(principal: str, grant_type: str = "readonly") -> dict:
    grant_type = grant_type.lower()
    if grant_type not in {"readonly", "readwrite"}:
        return {"ok": False, "error": f"非白名单授权类型: {grant_type}"}
    _GRANTS[principal] = {"grant_type": grant_type, "role": "dba-reader"}
    return {"ok": True, "principal": mask_principal(principal),
            "grant_type": grant_type, "note": "已在演示系统开通（仅本地内存）"}


@register_tool(name="revoke_db_readonly", risk="high", roles=("operator", "admin"),
               description="回收数据库只读权限（补偿动作，需审批）。")
def revoke_db_readonly(principal: str) -> dict:
    existed = _GRANTS.pop(principal, None)
    return {"ok": existed is not None, "principal": mask_principal(principal),
            "note": "已回收权限" if existed else "无该授权记录"}
