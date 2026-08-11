"""M1 冒烟测试：初始化 DB → 播种 → 登录 → 建单 → 跑低风险工单 → 校验状态与事件。"""
import os, sys, json
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.db import init_db
from app.api.auth import seed_users
from app.security import create_session
from app import daos
from app.orchestrator import run_ticket


def main():
    init_db()
    seed_users()
    token = create_session(1)  # user admin id=1 in seeded DB

    tid = daos.create_ticket(
        tenant_id=1, requester_id=1,
        title="VPN 连接不上", description="办公室网络下无法连接公司 VPN",
        risk_level="low", intent_type="knowledge",
    )
    daos.add_event(tid, "created", {"title": "VPN 连接不上"}, "admin")

    result = run_ticket(tid, actor="admin")
    assert result["status"] == "done", result

    ticket = daos.get_ticket(tid)
    events = daos.list_events(tid)
    print("RESULT:", json.dumps(result, ensure_ascii=False))
    print("TICKET:", dict(ticket))
    print("EVENTS:")
    for e in events:
        print("  -", e["event_type"], e["payload_json"])
    assert ticket["status"] == "done"
    assert any(e["event_type"] == "tool_call" for e in events)
    print("\nSMOKE OK")


if __name__ == "__main__":
    main()
