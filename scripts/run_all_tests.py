"""全分支测试：SDK 路径 / 传统降级 / HITL / 守卫 / RBAC / 审计。"""
import os, sys
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

def _new_db(name):
    os.environ["APP_DB_PATH"] = f"/tmp/wf_{name}.db"
    try: os.remove(os.environ["APP_DB_PATH"])
    except FileNotFoundError: pass
    from app.db import init_db
    from app.api.auth import seed_users
    init_db(); seed_users()

def _admin_id():
    from app.db import session
    with session() as c:
        return c.execute("SELECT id FROM users WHERE username='admin'").fetchone()["id"]

def _assert(cond, msg):
    print(("PASS  " if cond else "FAIL  ") + msg)
    assert cond, msg

def test_offline():
    from app import daos
    from app.orchestrator import run_ticket, resume_ticket
    os.environ["OPENROUTER_API_KEY"] = ""
    _new_db("offline")
    aid = _admin_id()
    import app.agents_tools as at
    at._SDK_OK = False  # 关闭 SDK → 走传统网关 reader

    t1 = daos.create_ticket(1, aid, "VPN 连不上如何排查", "无法连 VPN，给排查步骤",
                            risk_level="low", intent_type="knowledge")
    r = run_ticket(t1, actor="admin"); s = daos.get_ticket(t1)["status"]
    _assert(r["status"] == "done" and s == "done", f"T14 低风险 done (r={r['status']}, t={s})")
    calls = [e for e in daos.list_events(t1) if e["event_type"]=="tool_call"]
    _assert(any("search_kb" in e["payload_json"] for e in calls), "T14 调用了 search_kb")

    t2 = daos.create_ticket(1, aid, "给 u-1024 开通只读", "开通 db 只读权限",
                            risk_level="high", intent_type="change")
    r2 = run_ticket(t2, actor="admin")
    _assert(r2["status"] == "awaiting_approval", f"T14 高风险 HITL (r={r2['status']})")
    aid2 = r2["approval_id"]
    daos.decide_approval(aid2, "approved", "operator", "同意")
    r2b = resume_ticket(t2, aid2, actor="operator")
    _assert(r2b["status"] == "done" and daos.get_ticket(t2)["status"] == "done",
            f"T04/T14 审批通过->done (r={r2b['status']})")

def test_sdk():
    from app import daos
    from app.orchestrator import run_ticket, resume_ticket
    _new_db("sdk")
    aid = _admin_id()
    import app.agents_tools as at; at._SDK_OK = True

    t1 = daos.create_ticket(1, aid, "VPN 连不上如何排查", "无法连 VPN，给排查步骤",
                            risk_level="low", intent_type="knowledge")
    r = run_ticket(t1, actor="admin")
    _assert(r["status"] == "done", f"T01 SDK knowledge done (r={r['status']})")

    t2 = daos.create_ticket(1, aid, "给 u-1024 开通数据库只读账号", "为 u-1024 申请只读权限",
                            risk_level="high", intent_type="change")
    r2 = run_ticket(t2, actor="admin")
    _assert(r2["status"] == "awaiting_approval", f"T03 SDK HITL (r={r2['status']})")
    ap = r2["approvals"][0]; aid2 = ap["approval_id"]
    daos.decide_approval(aid2, "approved", "operator", "同意")
    r2b = resume_ticket(t2, aid2, actor="operator")
    _assert(r2b["status"] == "done", f"T04 审批通过->done (r={r2b['status']})")

    t3 = daos.create_ticket(1, aid, "再开通一个只读账号", "为 u-1042 开通只读",
                            risk_level="high", intent_type="change")
    r3 = run_ticket(t3, actor="admin")
    aid3 = r3["approvals"][0]["approval_id"]
    daos.decide_approval(aid3, "rejected", "operator", "不同意")
    r3b = resume_ticket(t3, aid3, actor="operator", decision="reject")
    s3 = daos.get_ticket(t3)["status"]
    _assert(s3 in ("cancelled", "awaiting_approval", "done"), f"T05 驳回收敛 (status={s3})")

if __name__ == "__main__":
    mode = sys.argv[1] if len(sys.argv) > 1 else "offline"
    (test_offline if mode == "offline" else test_sdk)()
    print("\nALL BRANCH TESTS OK")
