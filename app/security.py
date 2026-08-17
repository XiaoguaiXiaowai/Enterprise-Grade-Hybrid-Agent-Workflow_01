"""密码哈希、token、会话与会话校验（RBAC 校验在 auth.py 路由层）。"""
import hashlib
import hmac
import secrets
from datetime import datetime, timedelta

from .db import session
from .config import settings
from .tracelog import log_exception


def hash_password(password: str) -> str:
    salt = secrets.token_hex(16)
    digest = hashlib.pbkdf2_hmac("sha256", password.encode(), salt.encode(),
                                 settings.pbkdf2_iterations)
    return f"{salt}${digest.hex()}"


def verify_password(password: str, stored: str) -> bool:
    try:
        salt, hexdigest = stored.split("$", 1)
    except ValueError:
        log_exception()
        return False
    digest = hashlib.pbkdf2_hmac("sha256", password.encode(), salt.encode(),
                                 settings.pbkdf2_iterations)
    return hmac.compare_digest(digest.hex(), hexdigest)


def _hash_token(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


def create_session(user_id: int, ttl_hours: int | None = None) -> str:
    """创建会话；TTL 与 token 长度走配置（整改②：SESSION_TTL_HOURS / SESSION_TOKEN_BYTES）。"""
    ttl = ttl_hours if ttl_hours is not None else settings.session_ttl_hours
    token = secrets.token_urlsafe(settings.session_token_bytes)
    expires = datetime.now() + timedelta(hours=ttl)
    with session() as conn:
        conn.execute(
            "INSERT INTO sessions (user_id, token_hash, expires_at) VALUES (?,?,?)",
            (user_id, _hash_token(token), expires.isoformat()),
        )
    return token


def resolve_token(token: str) -> dict | None:
    """返回 {user_id, tenant_id, username, role} 或 None（无效/过期）。"""
    if not token:
        return None
    with session() as conn:
        row = conn.execute(
            """
            SELECT s.user_id, u.tenant_id, u.username, u.role, s.expires_at
            FROM sessions s JOIN users u ON u.id = s.user_id
            WHERE s.token_hash = ? AND u.is_active = 1
            """,
            (_hash_token(token),),
        ).fetchone()
    if not row:
        return None
    expires = datetime.fromisoformat(row["expires_at"])
    if expires < datetime.now():
        return None
    return {
        "user_id": row["user_id"],
        "tenant_id": row["tenant_id"],
        "username": row["username"],
        "role": row["role"],
    }
