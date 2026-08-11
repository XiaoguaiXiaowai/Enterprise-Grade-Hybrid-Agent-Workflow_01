"""FastAPI 依赖：当前用户上下文 + RBAC 校验。"""
from fastapi import Depends, Header, HTTPException, status

from .security import resolve_token


def current_user(x_auth_token: str | None = Header(default=None)):
    ctx = resolve_token(x_auth_token)
    if ctx is None:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="未登录或会话过期")
    return ctx


def require_role(*roles: str):
    def dep(ctx: dict = Depends(current_user)):
        if ctx["role"] not in roles:
            raise HTTPException(status_code=status.HTTP_403_FORBIDDEN,
                                detail=f"需要角色: {roles}")
        return ctx
    return dep
