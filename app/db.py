"""SQLite 连接与建表（单一写入口 DAO 由各模块负责，DB 只负责连接）。"""
import sqlite3
from contextlib import contextmanager
from pathlib import Path

from .config import settings
from .tracelog import log_exception


def connect() -> sqlite3.Connection:
    path: Path = settings.abs_db_path
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA journal_mode = WAL")
    # 架构改善阶段1-3：写锁等待 5s（降低并发下 database is locked）+ WAL 下
    # synchronous=NORMAL 保证崩溃一致性的同时减少 fsync 争用，写并发更友好
    conn.execute("PRAGMA busy_timeout = 5000")
    conn.execute("PRAGMA synchronous = NORMAL")
    return conn


@contextmanager
def session():
    conn = connect()
    try:
        yield conn
        conn.commit()
    except Exception:
        log_exception()
        conn.rollback()
        raise
    finally:
        conn.close()


SCHEMA = """
CREATE TABLE IF NOT EXISTS tenants (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT NOT NULL UNIQUE,
    created_at TEXT NOT NULL DEFAULT (datetime('now'))
);
CREATE TABLE IF NOT EXISTS users (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    tenant_id INTEGER NOT NULL REFERENCES tenants(id),
    username TEXT NOT NULL UNIQUE,
    password_hash TEXT NOT NULL,
    role TEXT NOT NULL DEFAULT 'employee',   -- employee | operator | admin
    is_active INTEGER NOT NULL DEFAULT 1,
    created_at TEXT NOT NULL DEFAULT (datetime('now'))
);
CREATE TABLE IF NOT EXISTS sessions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id INTEGER NOT NULL REFERENCES users(id),
    token_hash TEXT NOT NULL UNIQUE,
    expires_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS tickets (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    tenant_id INTEGER NOT NULL REFERENCES tenants(id),
    requester_id INTEGER NOT NULL REFERENCES users(id),
    title TEXT NOT NULL,
    description TEXT NOT NULL,
    risk_level TEXT NOT NULL,          -- low | medium | high
    intent_type TEXT NOT NULL DEFAULT 'knowledge',
    status TEXT NOT NULL DEFAULT 'created',
    priority TEXT NOT NULL DEFAULT 'normal',
    budget_used_steps INTEGER NOT NULL DEFAULT 0,
    created_at TEXT NOT NULL DEFAULT (datetime('now')),
    updated_at TEXT NOT NULL DEFAULT (datetime('now'))
);
CREATE TABLE IF NOT EXISTS ticket_events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ticket_id INTEGER NOT NULL REFERENCES tickets(id),
    event_type TEXT NOT NULL,
    payload_json TEXT,
    actor TEXT,
    created_at TEXT NOT NULL DEFAULT (datetime('now'))
);
CREATE TABLE IF NOT EXISTS tool_calls (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ticket_id INTEGER NOT NULL REFERENCES tickets(id),
    tool_name TEXT NOT NULL,
    params_json TEXT,
    result_json TEXT,
    status TEXT NOT NULL,              -- done | failed | pending_approval | approved | rejected
    idempotency_key TEXT,
    latency_ms INTEGER,
    retry_count INTEGER NOT NULL DEFAULT 0,
    created_at TEXT NOT NULL DEFAULT (datetime('now'))
);
CREATE TABLE IF NOT EXISTS approvals (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ticket_id INTEGER NOT NULL REFERENCES tickets(id),
    tool_name TEXT NOT NULL,
    params_json TEXT,
    requested_by TEXT,
    status TEXT NOT NULL DEFAULT 'pending',   -- pending | approved | rejected
    decided_by TEXT,
    decided_at TEXT,
    reason TEXT,
    created_at TEXT NOT NULL DEFAULT (datetime('now'))
);
CREATE TABLE IF NOT EXISTS traces (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ticket_id INTEGER NOT NULL REFERENCES tickets(id),
    req_id TEXT,
    model TEXT,
    provider TEXT,
    prompt_version TEXT,
    context_source TEXT,
    tool_name TEXT,
    io_json TEXT,
    state_before TEXT,
    state_after TEXT,
    latency_ms INTEGER,
    token_usage_json TEXT,
    retry_count INTEGER NOT NULL DEFAULT 0,
    created_at TEXT NOT NULL DEFAULT (datetime('now'))
);
CREATE TABLE IF NOT EXISTS memory (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    tenant_id INTEGER NOT NULL REFERENCES tenants(id),
    key TEXT NOT NULL,
    value_json TEXT,
    source TEXT,
    updated_at TEXT NOT NULL DEFAULT (datetime('now'))
);
CREATE TABLE IF NOT EXISTS kb_documents (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    filename TEXT NOT NULL,
    title TEXT NOT NULL,
    content TEXT NOT NULL,
    tag TEXT NOT NULL DEFAULT 'doc',
    created_at TEXT NOT NULL DEFAULT (datetime('now'))
);
CREATE TABLE IF NOT EXISTS metrics (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ticket_id INTEGER NOT NULL REFERENCES tickets(id),
    day TEXT NOT NULL,
    success INTEGER NOT NULL DEFAULT 0,
    correct_failure INTEGER NOT NULL DEFAULT 0,
    latency_ms INTEGER NOT NULL DEFAULT 0,
    cost REAL NOT NULL DEFAULT 0,
    human_takeover INTEGER NOT NULL DEFAULT 0,
    created_at TEXT NOT NULL DEFAULT (datetime('now'))
);
CREATE TABLE IF NOT EXISTS grants (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ticket_id INTEGER REFERENCES tickets(id),   -- 可空：工具调用不感知工单号，留审计关联位
    principal TEXT NOT NULL,
    grant_type TEXT NOT NULL,
    role TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'active',   -- active | revoked | expired
    expires_at TEXT,
    created_at TEXT NOT NULL DEFAULT (datetime('now'))
);
"""


def _migrate(conn: sqlite3.Connection) -> None:
    """轻量迁移：为新旧库补充 tickets 上的预算列（幂等）。"""
    cols = {r["name"] for r in conn.execute("PRAGMA table_info(tickets)")}
    if "budget_used_tokens" not in cols:
        conn.execute("ALTER TABLE tickets ADD COLUMN budget_used_tokens INTEGER NOT NULL DEFAULT 0")
    if "budget_used_seconds" not in cols:
        conn.execute("ALTER TABLE tickets ADD COLUMN budget_used_seconds REAL NOT NULL DEFAULT 0")
    if "prompt_version" not in cols:
        conn.execute("ALTER TABLE tickets ADD COLUMN prompt_version TEXT")
    if "trace_req_id" not in cols:
        conn.execute("ALTER TABLE tickets ADD COLUMN trace_req_id TEXT")
    if "final_answer" not in cols:
        conn.execute("ALTER TABLE tickets ADD COLUMN final_answer TEXT")
    # P2-11：审批跨重启恢复——approvals 表补 RunState 序列化与恢复所需列
    acols = {r["name"] for r in conn.execute("PRAGMA table_info(approvals)")}
    if "run_state_json" not in acols:
        conn.execute("ALTER TABLE approvals ADD COLUMN run_state_json TEXT")
    if "routing_json" not in acols:
        conn.execute("ALTER TABLE approvals ADD COLUMN routing_json TEXT")
    if "run_ctx_json" not in acols:
        conn.execute("ALTER TABLE approvals ADD COLUMN run_ctx_json TEXT")
    if "budget_json" not in acols:
        conn.execute("ALTER TABLE approvals ADD COLUMN budget_json TEXT")
    if "req_id" not in acols:
        conn.execute("ALTER TABLE approvals ADD COLUMN req_id TEXT")
    if "raw_item_json" not in acols:
        conn.execute("ALTER TABLE approvals ADD COLUMN raw_item_json TEXT")
    conn.commit()


def init_db() -> None:
    with session() as conn:
        conn.executescript(SCHEMA)
        _migrate(conn)
