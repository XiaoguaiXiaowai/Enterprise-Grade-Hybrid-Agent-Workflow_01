"""OpenAI Agents SDK 真实链路冒烟：OpenRouter + 工具循环 + 原生 HITL 审批恢复。

用法：.venv/bin/python scripts/smoke_agents.py
要求：已设置 OPENROUTER_API_KEY（.env 或环境变量），可访问 openrouter.ai
覆盖：
  1. 低风险知识工单 → SDK 工具循环 → done
  2. 高风险工单 → SDK 原生中断(interruptions) → awaiting_approval → 审批通过 → 恢复 → done
  3. 多 Agent handoff：trace 应含 TriageAgent 与对应子 Agent 的 agent span
  4. trace 落库 + 工具调用记录
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ.setdefault("APP_DB_PATH", "/tmp/agents_smoke.db")

from app.db import init_db
from app.api.auth import seed_users
from app import daos, trace as trace_mod
from app.orchestrator import run_ticket, resume_ticket
from app.skills import kb as _kb, user_dir as _ud, grant as _gr  # noqa: F401 注册工具


def _admin_id():
    import app.db
    with app.db.session() as conn:
        return conn.execute("SELECT id FROM users WHERE username='admin'").fetchone()["id"]


def _tool_calls(tid):
    import app.db
    with app.db.session() as conn:
        return [dict(x) for x in conn.execute(
            "SELECT id, tool_name, status FROM tool_calls WHERE ticket_id=?", (tid,)).fetchall()]


def _assert_agents_traced(tid, expected):
    """断言 SDK trace 的 agent span 中含期望的 Agent 名（验证 handoff 多 Agent 链路）。"""
    import json as _json
    found = set()
    for tr in trace_mod.list_for_ticket(tid):
        try:
            io = _json.loads(tr["io_json"] or "{}")
        except Exception:
            continue
        for sp in io.get("spans", []):
            if sp.get("type") == "agent" and sp.get("name"):
                found.add(sp["name"])
    missing = [e for e in expected if e not in found]
    print(f"  已 trace 的 Agent: {sorted(found)}")
    assert not missing, f"期望的 Agent span 未出现: {missing}"


def main():
    os.environ["APP_DB_PATH"] = "/tmp/agents_smoke.db"
    init_db()
    seed_users()
    admin_id = _admin_id()

    # ---- 场景 1：低风险知识工单 ----
    tid = daos.create_ticket(1, admin_id, "VPN 连接不上如何排查",
                             "用户反馈办公室无法连接公司 VPN，帮忙查知识库给出排查步骤",
                             risk_level="low", intent_type="knowledge")
    r = run_ticket(tid, actor="admin")
    t = daos.get_ticket(tid)
    print(f"[场景1] id={tid} result={r['status']} status={t['status']}")
    assert r["status"] == "done" and t["status"] == "done", r
    print("  工具调用:", _tool_calls(tid))
    assert len(trace_mod.list_for_ticket(tid)) > 0, "trace 未写入"
    _assert_agents_traced(tid, ("TriageAgent", "KnowledgeAgent"))

    # ---- 场景 2：高风险工单 → SDK 原生中断 → 审批恢复 ----
    tid2 = daos.create_ticket(1, admin_id, "给 u-1024 开通数据库只读账号",
                              "为 u-1024 申请数据库只读权限", risk_level="high",
                              intent_type="change")
    r2 = run_ticket(tid2, actor="admin")
    t2 = daos.get_ticket(tid2)
    print(f"[场景2] result={r2}")
    assert r2["status"] == "awaiting_approval", r2
    assert t2["status"] == "awaiting_approval", t2

    ap_list = r2["approvals"]
    assert ap_list, "无审批记录"
    aid = ap_list[0]["approval_id"]
    print("  审批单:", {**dict(daos.get_approval(aid))})
    assert daos.get_approval(aid)["status"] == "pending"

    # 审批通过并恢复（走 SDK resume）
    daos.decide_approval(aid, "approved", "operator", "同意")
    r2b = resume_ticket(tid2, aid, actor="operator")
    t2b = daos.get_ticket(tid2)
    print(f"[场景2 恢复] result={r2b}")
    assert r2b["status"] == "done", r2b
    assert t2b["status"] == "done", t2b
    _assert_agents_traced(tid2, ("TriageAgent", "ChangeAgent"))

    print("\n场景1 trace:")
    for tr in trace_mod.list_for_ticket(tid):
        print("  ", tr["provider"], tr["model"], "|", tr["context_source"], "|", str(tr["io_json"])[:100])
    print("场景2 trace:", len(trace_mod.list_for_ticket(tid2)), "条")
    print("\nAGENTS SMOKE OK")


if __name__ == "__main__":
    main()
