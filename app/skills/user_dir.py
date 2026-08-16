"""只读工具：用户目录/权限查询（低-中风险），数据源为真实 users 表，返回前脱敏。

- principal 支持两种格式：u-{id}（按主键查）或登录用户名（按 username 查）
- 返回脱敏后的用户信息与角色（users.role → roles 列表，保持返回结构兼容）
- 此前是硬编码 2 个用户的字典，现接入认证体系同一张 users 表
"""
from ..db import session
from .registry import register_tool
from .masker import mask_principal


@register_tool(name="query_user_dir", risk="medium",
               description="查询用户目录中的用户信息与权限（只读，返回脱敏）。")
def query_user_dir(principal: str) -> dict:
    if not principal:
        return {"found": False, "principal": ""}
    with session() as conn:
        if principal.startswith("u-") and principal[2:].isdigit():
            row = conn.execute(
                "SELECT id, username, role, is_active FROM users WHERE id=?", (int(principal[2:]),)
            ).fetchone()
        else:
            row = conn.execute(
                "SELECT id, username, role, is_active FROM users WHERE username=?", (principal,)
            ).fetchone()
    if row is None or not row["is_active"]:
        return {"found": False, "principal": mask_principal(principal)}
    return {"found": True, "principal": mask_principal(principal),
            "name": mask_principal(row["username"]),
            "email": "",  # users 表无邮箱字段，预留兼容位
            "roles": [row["role"]], "source": "users_table",
            "user_id": row["id"]}
