"""OpenAI Agents SDK 真实链路冒烟：OpenRouter 主链路 + trace 落库 + 工具调用。

用法：.venv/bin/python scripts/smoke_agents.py
要求：已设置 OPENROUTER_API_KEY（.env 或环境变量），可访问 openrouter.ai
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ.setdefault("APP_DB_PATH", "/tmp/agents_smoke.db")

from app.db import init_db
from app.api.auth import seed_users
from app.security import create_session
from app import daos, trace as trace_mod
from app.orchestrator import run_ticket
from app.skills import kb as _kb, user_dir as _ud, grant as _gr  # noqa: F401 注册工具


def main():
    init_db()
    seed_users()
    with __import__("app.db", fromlist=["session"]).session() as conn:
        admin_id = conn.execute("SELECT id FROM users WHERE username='admin'").fetchone()["id"]
    token = create_session(admin_id)

    # 场景：低风险知识工单（走 OpenRouter 真实模型调用）
    tid = daos.create_ticket(1, admin_id, "VPN 连接不上如何排查",
                             "用户反馈办公室无法连接公司 VPN，帮忙查知识库给出排查步骤",
                             risk_level="low", intent_type="knowledge")
    print(f"[创建工单] id={tid}")
    r = run_ticket(tid, actor="admin")
    t = daos.get_ticket(tid)
    print("[运行结果]", r)
    print("[工单状态]", t["status"])
    assert r["status"] == "done", f"工单未完成: {r}"
    assert t["status"] == "done"

    n_trace = len(trace_mod.list_for_ticket(tid))
    print("[trace 条数]", n_trace)
    assert n_trace > 0, "trace 未写入"

    calls = []
    with __import__("app.db", fromlist=["session"]).session() as conn:
        calls = [dict(x) for x in conn.execute(
            "SELECT id, tool_name, status, latency_ms, result_json FROM tool_calls WHERE ticket_id=?",
            (tid,)).fetchall()]
    print("[工具调用]", calls)

    for tr in trace_mod.list_for_ticket(tid):
        print("  trace>", tr["provider"], tr["model"], "|", tr["context_source"],
              "|", str(tr["io_json"])[:120])

    print("\nAGENTS SMOKE OK")


if __name__ == "__main__":
    main()
