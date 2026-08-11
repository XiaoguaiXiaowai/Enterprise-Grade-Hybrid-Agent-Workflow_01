"""认证路由：登录（本地账户）、当前用户、种子演示账户（M1 简化，SSO 迁移点见文档）。"""
from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel

from ..db import session
from ..security import hash_password, verify_password, create_session
from ..deps import current_user

router = APIRouter(prefix="/api/auth", tags=["auth"])


class LoginIn(BaseModel):
    username: str
    password: str


class LoginOut(BaseModel):
    token: str
    user: dict


@router.post("/login", response_model=LoginOut)
def login(body: LoginIn):
    with session() as conn:
        row = conn.execute("SELECT * FROM users WHERE username = ?", (body.username,)).fetchone()
    if not row or not verify_password(body.password, row["password_hash"]):
        raise HTTPException(status_code=401, detail="用户名或密码错误")
    token = create_session(row["id"])
    return LoginOut(token=token, user={"id": row["id"], "username": row["username"],
                                       "role": row["role"], "tenant_id": row["tenant_id"]})


@router.get("/me")
def me(ctx: dict = Depends(current_user)):
    return ctx


@router.post("/seed")
def seed_users():
    """M1 演示用：确保存在默认租户与三种角色账户。生产移除。"""
    with session() as conn:
        conn.execute("INSERT OR IGNORE INTO tenants (id, name) VALUES (1, '默认租户')")
        defaults = [
            ("employee", "pass123"),
            ("operator", "pass123"),
            ("admin", "pass123"),
        ]
        for role, pw in defaults:
            cur = conn.execute("SELECT id FROM users WHERE username = ?", (role,))
            if cur.fetchone() is None:
                conn.execute(
                    "INSERT INTO users (tenant_id, username, password_hash, role) VALUES (1,?,?,?)",
                    (role, hash_password(pw), role),
                )
    return {"seeded": True}
