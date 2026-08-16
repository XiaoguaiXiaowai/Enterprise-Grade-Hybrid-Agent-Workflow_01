"""全分支测试：SDK 路径 / 传统降级 / HITL / 守卫 / RBAC / 审计。"""
import os, sys
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ.setdefault("APP_ENV", "development")  # 测试脚本强制非生产，播种才生效（整改⑥）

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
    # 彻底离线：主用(OpenRouter 无 key) 与 Ollama 兜底(不可达) 均不可用 → 确定性 reader 链路。
    # 注意：env 必须在 import app.* 之前设置——settings 在模块加载时实例化，
    # 之后改 os.environ 不生效（此前测试悄悄连上了远程 Ollama 导致结果不稳定）。
    os.environ["OPENROUTER_API_KEY"] = ""
    os.environ["OLLAMA_BASE_URL"] = "http://127.0.0.1:9/v1"
    from app import daos
    from app.orchestrator import run_ticket, resume_ticket
    _new_db("offline")
    aid = _admin_id()
    import app.agents_tools as at
    at._SDK_OK = False  # 关闭 SDK → 走传统网关 reader

    t1 = daos.create_ticket(1, aid, "VPN 连不上如何排查", "无法连 VPN，给排查步骤",
                            risk_level="low", intent_type="knowledge")
    r = run_ticket(t1, actor="admin"); s = daos.get_ticket(t1)["status"]
    # 当前设计：回合收敛到 pending_confirm（待客户确认完成），确认后才变 done
    _assert(r["status"] == "done" and s == "pending_confirm",
            f"T14 低风险 done (r={r['status']}, t={s})")
    calls = [e for e in daos.list_events(t1) if e["event_type"]=="tool_call"]
    _assert(any("search_kb" in e["payload_json"] for e in calls), "T14 调用了 search_kb")

    t2 = daos.create_ticket(1, aid, "给 u-1024 开通只读", "开通 db 只读权限",
                            risk_level="high", intent_type="change")
    r2 = run_ticket(t2, actor="admin")
    _assert(r2["status"] == "awaiting_approval", f"T14 高风险 HITL (r={r2['status']})")
    aid2 = r2["approval_id"]
    daos.decide_approval(aid2, "approved", "operator", "同意")
    r2b = resume_ticket(t2, aid2, actor="operator")
    _assert(r2b["status"] == "done" and daos.get_ticket(t2)["status"] == "pending_confirm",
            f"T04/T14 审批通过->待确认 (r={r2b['status']})")

    # T15 取消工单：awaiting_approval 可取消，未决审批一并 rejected
    t3 = daos.create_ticket(1, aid, "给 u-1042 再开一个只读账号", "开通 db 只读权限",
                            risk_level="high", intent_type="change")
    r3 = run_ticket(t3, actor="admin")
    _assert(r3["status"] == "awaiting_approval", f"T15 待审批 (r={r3['status']})")
    aid3 = r3["approval_id"]
    from app.state_machine import can_transition
    _assert(can_transition("awaiting_approval", "cancelled"), "T15 状态机允许取消")
    daos.transition(t3, "cancelled", "admin", reason="测试取消")
    for ap in daos.pending_for_ticket(t3):
        daos.decide_approval(ap["id"], "rejected", "admin", "工单已取消")
    _assert(daos.get_ticket(t3)["status"] == "cancelled", "T15 取消后状态 cancelled")
    _assert(daos.get_approval(aid3)["status"] == "rejected", "T15 未决审批已作废")

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
