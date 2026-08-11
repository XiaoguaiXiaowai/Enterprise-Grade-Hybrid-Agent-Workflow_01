"""M2 冒烟测试：低风险工单 + 高风险工单(HITL) + Trace + 指标 全链路。"""
import os, sys, json
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ.setdefault("APP_DB_PATH", "/tmp/m2_smoke.db")

from app.db import init_db
from app.api.auth import seed_users
from app.security import create_session
from app import daos, trace as trace_mod, metrics as metrics_mod
from app.orchestrator import run_ticket, resume_ticket


def main():
    init_db()
    seed_users()
    admin_id = daos_session_query_admin()
    token = create_session(admin_id)

    # ---- 场景 1：低风险知识工单 ----
    tid1 = daos.create_ticket(1, admin_id, "VPN 连接不上", "办公室无法连接公司 VPN",
                              risk_level="low", intent_type="knowledge")
    r1 = run_ticket(tid1, actor="admin")
    t1 = daos.get_ticket(tid1)
    print("[场景1 低风险]", r1["status"], "->", t1["status"])
    assert r1["status"] == "done", r1
    assert t1["status"] == "done"
    n_trace1 = len(trace_mod.list_for_ticket(tid1))
    print("  trace 条数:", n_trace1); assert n_trace1 > 0

    # ---- 场景 2：高风险变更工单 → HITL → 审批 → 恢复 ----
    tid2 = daos.create_ticket(1, admin_id, "给 u-1024 开通数据库只读账号",
                              "为 u-1024 申请数据库只读权限", risk_level="high", intent_type="change")
    r2 = run_ticket(tid2, actor="admin")
    print("[场景2 高风险]", r2)
    assert r2["status"] == "awaiting_approval", r2
    aid = r2["approval_id"]

    ap = daos.get_approval(aid)
    print("  审批单:", dict(ap)); assert ap["status"] == "pending"

    # 审批通过并恢复
    daos.decide_approval(aid, "approved", "operator", "同意")
    r2b = resume_ticket(tid2, aid, actor="operator")
    t2 = daos.get_ticket(tid2)
    print("[场景2 恢复]", r2b["status"], "->", t2["status"])
    assert r2b["status"] == "done", r2b
    assert t2["status"] == "done"

    print("\n[指标]", metrics_mod.aggregate())
    print("M2 SMOKE OK")


def daos_session_query_admin():
    # 返回 admin 用户 id（seed 后 id 通常=3）
    from app.db import session
    with session() as conn:
        row = conn.execute("SELECT id FROM users WHERE username='admin'").fetchone()
    return row["id"]


if __name__ == "__main__":
    main()
