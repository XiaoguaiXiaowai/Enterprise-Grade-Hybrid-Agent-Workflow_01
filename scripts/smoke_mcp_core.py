"""MCP 确定性冒烟（M5-P0）：不依赖真模型，验证 MCP 工具面的治理链路。

与 scripts/smoke_mcp.py（真模型端到端，best-effort）互补：本脚本确定性必过，
覆盖「连接/装配/白名单/治理执行/审计落库/trace 溯源/脱敏/预算/管理 API」全部断言。

用法：.venv/bin/python scripts/smoke_mcp_core.py
"""
import json
import os
import sys
import tempfile
from types import SimpleNamespace

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ.setdefault("APP_ENV", "development")
os.environ.setdefault("CMDB_DATA", "/tmp/mcp_cmdb_core.json")

_tmp = tempfile.mktemp(suffix=".db")
os.environ["APP_DB_PATH"] = _tmp

import app.db as db
from app.db import init_db
from app.api.auth import seed_users
from app import daos, trace as trace_mod
from app.budget import Budget
from app.mcp import servers as mcp_servers
from app.mcp import bridge
from app.mcp.config import load_config
from app.agents_tools import build_mcp_tools

_PASS = 0
_FAIL = 0


def _assert(cond, msg):
    global _PASS, _FAIL
    if cond:
        _PASS += 1
        print("PASS  " + msg)
    else:
        _FAIL += 1
        print("FAIL  " + msg)


def _admin_id():
    with db.session() as conn:
        return conn.execute("SELECT id FROM users WHERE username='admin'").fetchone()["id"]


def main():
    init_db()
    seed_users()
    admin_id = _admin_id()

    # ---- 1) 配置映射表（白名单唯一事实源）----
    cfg = load_config().get("cmdb")
    _assert(cfg is not None, "M1 配置加载：cmdb server 存在")
    _assert(sorted(cfg.tools) == ["list_network_devices", "query_asset", "update_asset_owner"],
            "M2 映射表包含 3 个显式声明的工具")
    _assert(cfg.tools["update_asset_owner"].risk == "high",
            "M3 写操作映射为 high 风险 -> HITL")
    _assert("employee" not in cfg.tools["update_asset_owner"].roles,
            "M4 employee 角色无写操作权限")

    # ---- 2) 装配与白名单（不触发连接也可断言角色/命名）----
    emp = {t.name for t in build_mcp_tools("employee", ["cmdb__query_asset",
                                                        "cmdb__update_asset_owner"])}
    _assert(emp == {"cmdb__query_asset"}, f"M5 employee 只装配只读工具: {emp}")
    op = {t.name for t in build_mcp_tools("operator", ["cmdb__query_asset",
                                                       "cmdb__update_asset_owner"])}
    _assert(op == {"cmdb__query_asset", "cmdb__update_asset_owner"},
            f"M6 operator 装配只读+写工具: {op}")
    # HITL 接线（确定性保证，不依赖真模型）：高风险 MCP 工具在装配层即为 needs_approval=True
    ft_high = [t for t in build_mcp_tools("operator", ["cmdb__update_asset_owner"])]
    _assert(len(ft_high) == 1 and ft_high[0].needs_approval is True,
            "M6b 高风险 MCP 工具 needs_approval=True（装配即接入 SDK 原生 HITL）")
    ft_low = [t for t in build_mcp_tools("operator", ["cmdb__query_asset"])]
    _assert(len(ft_low) == 1 and ft_low[0].needs_approval is False,
            "M6c 只读 MCP 工具 needs_approval=False（不需审批）")
    unk = {t.name for t in build_mcp_tools("operator", ["cmdb__drop_database"])}
    _assert(unk == set(), "M7 未映射工具拒绝装配（白名单生效）")

    # ---- 3) 连接 + 工具清单（真实 MCP stdio 子进程）----
    conn = mcp_servers.registry.get_connection("cmdb")
    _assert(conn.healthy, f"M8 连接成功（error={conn.error[:100] if conn.error else ''}）")
    _assert(set(conn.tools) == {"query_asset", "list_network_devices", "update_asset_owner"},
            f"M9 发现的工具全集: {sorted(conn.tools)}")

    # ---- 4) 治理执行体：只读调用 → 落库/trace/脱敏/预算 ----
    tid = daos.create_ticket(1, admin_id, "查 db-01 负责人", "查 db-01 的负责人", "low", "knowledge")
    budget = Budget.start_for(daos.get_ticket(tid))
    ctx = SimpleNamespace(context={
        "ticket_id": tid, "actor": "admin", "budget": budget,
        "req_id": "", "model": "test-model", "provider": "openrouter",
    })
    steps0 = budget.steps
    r = bridge._invoke_mcp(ctx, "cmdb", "query_asset", {"hostname": "db-01"})
    _assert(isinstance(r, dict) and r.get("asset", {}).get("hostname") == "db-01",
            f"M10 只读调用返回资产: {str(r)[:120]}")
    _assert(budget.steps == steps0 + 1, "M11 预算步数 +1（治理计步）")

    calls = []
    with db.session() as conn:
        calls = [dict(x) for x in conn.execute(
            "SELECT tool_name, status FROM tool_calls WHERE ticket_id=?", (tid,))]
    _assert(any(c["tool_name"] == "cmdb__query_asset" and c["status"] == "done"
                for c in calls), f"M12 tool_calls 落库: {calls}")
    mcp_traces = [tr for tr in trace_mod.list_for_ticket(tid)
                  if tr["context_source"] == "mcp"]
    _assert(mcp_traces, "M13 trace 落库且 context_source=mcp")

    # 脱敏：3 字中文名会被打码（owner 字段；用不同参数避免与重复拦截冲突）
    r3 = bridge._invoke_mcp(ctx, "cmdb", "query_asset", {"hostname": "db-02"})
    owner = r3.get("asset", {}).get("owner", "")
    _assert(isinstance(r3, dict) and "*" in str(owner),
            f"M15 输出脱敏（owner 打码）: {owner}")

    # ---- 5) 重复动作 / 未映射调用被拒绝 ----
    r_dup = bridge._invoke_mcp(ctx, "cmdb", "query_asset", {"hostname": "db-01"})
    _assert(isinstance(r_dup, dict) and r_dup.get("status") == "error"
            and "重复" in str(r_dup.get("error", "")),
            f"M16 重复动作拦截（is_duplicate 生效）: {str(r_dup)[:130]}")
    r_unknown = bridge._invoke_mcp(ctx, "cmdb", "drop_database", {})
    _assert(isinstance(r_unknown, dict) and r_unknown.get("status") == "error",
            "M17 未映射工具调用被拒")

    # ---- 6) 管理 API 数据（状态/脱敏）----
    st = mcp_servers.registry.status()
    _assert(any(s["name"] == "cmdb" and s["healthy"] for s in st), "M18 /api/mcp 状态含 healthy cmdb")
    red = mcp_servers.registry.list_server_configs()
    _assert(red and all(v == "***" for v in red[0]["headers"].values()),
            "M19 配置脱敏（headers 不泄漏）")

    # ---- 7) 生命周期：断开与重连 ----
    mcp_servers.registry.disconnect("cmdb")
    st2 = mcp_servers.registry.status()
    _assert(not st2[0]["healthy"], "M20 断开后 unhealthy(not_connected 语义)")
    conn2 = mcp_servers.registry.get_connection("cmdb")   # 懒重连
    _assert(conn2.healthy, "M21 下次调用自动重连")
    mcp_servers.registry.disconnect_all()

    print(f"\nMCP CORE SMOKE: {_PASS} passed, {_FAIL} failed")
    sys.exit(1 if _FAIL else 0)


if __name__ == "__main__":
    main()