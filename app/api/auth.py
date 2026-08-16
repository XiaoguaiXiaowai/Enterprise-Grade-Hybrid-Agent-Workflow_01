"""认证路由：登录（本地账户）、当前用户、种子演示账户（M1 简化，SSO 迁移点见文档）。

整改⑥：自动播种仅非生产生效（APP_ENV=production 直接跳过）；种子密码不再写死
pass123 —— 优先取 SEED_USER_PASSWORD 环境变量，缺省随机生成。
"""
import os
import secrets

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel

from ..config import settings
from ..db import session
from ..security import hash_password, verify_password, create_session
from ..deps import current_user
from ..tracelog import trace_call

router = APIRouter(prefix="/api/auth", tags=["auth"])

# 演示播种的角色（生产不自动创建）
SEED_ROLES = ("employee", "operator", "admin")

# 本次进程实际播种的演示密码（仅内存、仅演示环境记录，供登录页提示用）
_seeded_password: str | None = None


def _seed_password() -> str:
    """种子密码：SEED_USER_PASSWORD 环境变量优先，缺省随机生成（不再硬编码）。"""
    return os.getenv("SEED_USER_PASSWORD", "") or secrets.token_urlsafe(12)


class LoginIn(BaseModel):
    username: str
    password: str


class LoginOut(BaseModel):
    token: str
    user: dict


class SeedInfoOut(BaseModel):
    seeded: bool
    password: str | None = None
    roles: list[str] = []


@router.post("/login", response_model=LoginOut)
@trace_call("api.auth.login")
def login(body: LoginIn):
    with session() as conn:
        row = conn.execute("SELECT * FROM users WHERE username = ?", (body.username,)).fetchone()
    if not row or not verify_password(body.password, row["password_hash"]):
        raise HTTPException(status_code=401, detail="用户名或密码错误")
    token = create_session(row["id"])
    return LoginOut(token=token, user={"id": row["id"], "username": row["username"],
                                       "role": row["role"], "tenant_id": row["tenant_id"]})


@router.get("/me")
@trace_call("api.auth.me")
def me(ctx: dict = Depends(current_user)):
    return ctx


@router.get("/seed-info", response_model=SeedInfoOut)
@trace_call("api.auth.seed_info")
def seed_info():
    """演示环境：向登录页提供已播种账户与密码；生产环境一律不返回密码。"""
    if settings.env == "production":
        return SeedInfoOut(seeded=False)
    return SeedInfoOut(seeded=_seeded_password is not None,
                       password=_seeded_password, roles=list(SEED_ROLES))


@router.post("/seed")
@trace_call("api.auth.seed_users")
def seed_users():
    """M1 演示用：确保默认租户与三种角色账户。

    生产环境（APP_ENV=production）不生效 —— 演示账户播种是弱口令安全风险，
    生产请用真实用户体系或手动初始化（见 doc/硬编码清单.md 整改⑥）。
    """
    global _seeded_password
    if settings.env == "production":
        _seeded_password = None
        return {"seeded": False, "reason": "APP_ENV=production 禁用演示账户自动播种"}
    pw = _seed_password()
    _seeded_password = pw
    with session() as conn:
        conn.execute("INSERT OR IGNORE INTO tenants (id, name) VALUES (1, '默认租户')")
        for role in SEED_ROLES:
            cur = conn.execute("SELECT id FROM users WHERE username = ?", (role,))
            if cur.fetchone() is None:
                conn.execute(
                    "INSERT INTO users (tenant_id, username, password_hash, role) VALUES (1,?,?,?)",
                    (role, hash_password(pw), role),
                )
    return {"seeded": True}