"""监控指标：每次工单结束记录成功率/正确失败率/延迟/成本/人工接管率。"""
from datetime import date

from .db import session


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
    day = day or date.today().isoformat()
    with session() as conn:
        row = conn.execute(
            """SELECT COUNT(*) n,
                      SUM(success) success, SUM(correct_failure) cf,
                      AVG(latency_ms) avg_latency, SUM(cost) cost,
                      SUM(human_takeover) ht
               FROM metrics WHERE day = ?""", (day,)).fetchone()
    n = row["n"] or 0
    return {
        "day": day,
        "tickets": n,
        "success_rate": round((row["success"] or 0) / n, 4) if n else 0,
        "correct_failure_rate": round((row["cf"] or 0) / n, 4) if n else 0,
        "avg_latency_ms": round(row["avg_latency"] or 0, 1),
        "total_cost": round(row["cost"] or 0, 4),
        "human_takeover_rate": round((row["ht"] or 0) / n, 4) if n else 0,
    }
