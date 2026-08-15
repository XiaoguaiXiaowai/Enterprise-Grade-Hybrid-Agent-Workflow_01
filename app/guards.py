"""守卫：重复动作检测、重试、停止条件、关键节点确认。

M2 覆盖：
- duplicate_action：同一（工具+参数指纹）在时间窗内已成功 → 拦截
- idempotency_key：生成与校验（见 skills/base）
- should_stop：LLM 输出 done:true 或达到预算 → 停止
"""
import hashlib
import json
from datetime import datetime, timedelta

from .db import session
from .config import settings

# 去重窗口（分钟）外部化（整改②：DEDUP_WINDOW_MINUTES）
WINDOW_MINUTES = settings.dedup_window_minutes


def fingerprint(scope: str, tool_name: str, params: dict) -> str:
    raw = json.dumps(params, sort_keys=True, ensure_ascii=False, default=str)
    return hashlib.sha256(f"{scope}:{tool_name}:{raw}".encode()).hexdigest()


def is_duplicate(ticket_id: int, tool_name: str, params: dict) -> bool:
    """时间窗内该工单已成功执行过相同调用 → 视为重复。"""
    fprint = fingerprint(f"ticket:{ticket_id}", tool_name, params)
    since = (datetime.now() - timedelta(minutes=WINDOW_MINUTES)).isoformat()
    with session() as conn:
        row = conn.execute(
            """SELECT COUNT(*) c FROM tool_calls
               WHERE ticket_id=? AND idempotency_key=? AND status='done'
                 AND created_at >= ?
               GROUP BY idempotency_key""",
            (ticket_id, fprint, since)).fetchone()
    return bool(row and row["c"] > 0)


def should_stop(plan_text: str) -> bool:
    """收敛信号：模型明确输出 done:true。"""
    return "done:true" in plan_text.lower().replace(" ", "")
