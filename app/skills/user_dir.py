"""只读工具：用户目录/权限查询（低-中风险），返回前脱敏。"""
from .registry import register_tool
from .masker import mask_principal

_USERS = {
    "u-1001": {"name": "张三", "email": "zhangsan@corp.com", "roles": ["employee"]},
    "u-1024": {"name": "李四", "email": "lisi@corp.com", "roles": ["dba"]},
}


@register_tool(name="query_user_dir", risk="medium", description="查询用户目录中的用户信息与权限（只读，返回脱敏）。")
def query_user_dir(principal: str) -> dict:
    user = _USERS.get(principal)
    if not user:
        return {"found": False, "principal": mask_principal(principal)}
    return {"found": True, "principal": mask_principal(principal),
            "name": mask_principal(user["name"]), "email": mask_principal(user["email"]),
            "roles": user["roles"], "source": "local_dir"}
