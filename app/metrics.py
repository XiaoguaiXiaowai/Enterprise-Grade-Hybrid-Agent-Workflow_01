"""监控指标：每次工单结束记录成功率/正确失败率/延迟/成本/人工接管率。

支持按时间段（start/end，YYYY-MM-DD）与全部历史聚合，并可按天序列化，
供系统监控评估页面的时间筛选与趋势展示使用。
"""
from datetime import date

from .db import session

# 按天分组的原始聚合查询（不带 WHERE/GROUP BY，由各函数按需拼接）
_DAY_AGG_SQL = """
SELECT day,
       COUNT(*) n,
       SUM(success) success,
       SUM(correct_failure) cf,
       AVG(latency_ms) avg_latency,
       SUM(cost) cost,
       SUM(human_takeover) ht
FROM metrics
"""


def _where_range(start: str | None, end: str | None) -> tuple[str, tuple]:
    """构造时间段过滤的 WHERE 子句与参数；None 表示该侧不设界（全部历史）。"""
    if start and end:
        return " WHERE day BETWEEN ? AND ?", (start, end)
    if start:
        return " WHERE day >= ?", (start,)
    if end:
        return " WHERE day <= ?", (end,)
    return "", ()


def _summarize(row) -> dict:
    """把一行聚合结果规整为监控指标字典（tickets 为 0 时比率归零）。"""
    n = row["n"] or 0
    return {
        "tickets": n,
        "success_rate": round((row["success"] or 0) / n, 4) if n else 0,
        "correct_failure_rate": round((row["cf"] or 0) / n, 4) if n else 0,
        "avg_latency_ms": round(row["avg_latency"] or 0, 1),
        "total_cost": round(row["cost"] or 0, 4),
        "human_takeover_rate": round((row["ht"] or 0) / n, 4) if n else 0,
    }


def record(ticket_id, *, success, correct_failure, latency_ms, cost, human_takeover):
    day = date.today().isoformat()
    with session() as conn:
        conn.execute(
            """INSERT INTO metrics
               (ticket_id, day, success, correct_failure, latency_ms, cost, human_takeover)
               VALUES (?,?,?,?,?,?,?)""",
            (ticket_id, day, 1 if success else 0, 1 if correct_failure else 0,
             latency_ms, cost, 1 if human_takeover else 0),
        )


def aggregate(day: str | None = None):
    """单日汇总（向后兼容：缺省为今天）。"""
    day = day or date.today().isoformat()
    with session() as conn:
        row = conn.execute(_DAY_AGG_SQL + " WHERE day = ?", (day,)).fetchone()
    return {"day": day, **_summarize(row)}


def aggregate_range(start: str | None = None, end: str | None = None):
    """时间段汇总：start/end 为 None 表示该侧不设界（可覆盖全部历史）。"""
    where, params = _where_range(start, end)
    with session() as conn:
        row = conn.execute(_DAY_AGG_SQL + where, params).fetchone()
    return {"start": start, "end": end, **_summarize(row)}


def daily_series(start: str | None = None, end: str | None = None) -> list[dict]:
    """时间段内按天分组的指标序列（供趋势图）；None 表示覆盖全部历史。"""
    where, params = _where_range(start, end)
    with session() as conn:
        rows = conn.execute(
            _DAY_AGG_SQL + where + " GROUP BY day ORDER BY day", params).fetchall()
    return [{"day": r["day"], **_summarize(r)} for r in rows]


def available_range() -> dict:
    """表内最早/最晚记录日期，用于前端渲染筛选范围（无数据时均为 None）。"""
    with session() as conn:
        row = conn.execute("SELECT MIN(day) mn, MAX(day) mx FROM metrics").fetchone()
    return {"min": row["mn"], "max": row["mx"]}
