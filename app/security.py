"""密码哈希、token、会话与会话校验（RBAC 校验在 auth.py 路由层）。"""
import hashlib
import hmac
import secrets
from datetime import datetime, timedelta

from .db import session
from .config import settings


def hash_password(password: str) -> str:
    salt = secrets.token_hex(16)
    digest = hashlib.pbkdf2_hmac("sha256", password.encode(), salt.encode(), 100_000)
    return f"{salt}${digest.hex()}"


def verify_password(password: str, stored: str) -> bool:
    try:
        salt, hexdigest = stored.split("$", 1)
    except ValueError:
        return False
    digest = hashlib.pbkdf2_hmac("sha256", password.encode(), salt.encode(), 100_000)
    return hmac.compare_digest(digest.hex(), hexdigest)


def _hash_token(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


def create_session(user_id: int, ttl_hours: int = 12) -> str:
    token = secrets.token_urlsafe(32)
    expires = datetime.now() + timedelta(hours=ttl_hours)
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
