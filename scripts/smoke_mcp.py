"""MCP 集成冒烟（M5-P0）：真实链路验证 MCP 工具面接入与治理。

用法：.venv/bin/python scripts/smoke_mcp.py
要求：已设置 OPENROUTER_API_KEY（.env 或环境变量），可访问 openrouter.ai；
      依赖 scripts/mcp_servers/cmdb_server.py（stdio 子进程，SDK MCPServerStdio 拉起）。

覆盖：
  1. 装配与白名单：MCP 工具经映射表过滤装配（角色过滤 / 未映射拒绝），命名 {server}__{tool}
  2. 低风险只读工单 → CmdbAgent handoff → cmdb__query_asset → 收敛 done；
     trace 出现 context_source=mcp 的工具记录；tool_calls 落库
  3. 高风险变更工单 → cmdb__update_asset_owner（needs_approval=True）→ SDK 原生中断
     → awaiting_approval → 审批通过 → resume 恢复 → done；CMDB 数据真实变更
  4. 治理口径：MCP 工具调用计入 tool_calls 审计（status=done/failed）

测试数据隔离：CMDB_DATA=/tmp/mcp_cmdb_smoke.json（子进程继承环境变量），
不影响仓库 data/ 的真实演示数据。
"""
import json as _json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ.setdefault("APP_ENV", "development")
os.environ.setdefault("APP_DB_PATH", "/tmp/mcp_smoke.db")
os.environ.setdefault("CMDB_DATA", "/tmp/mcp_cmdb_smoke.json")
# 显式注入分类/重写模型：若 .env 未配置，空模型会让 OpenRouter 400 后 fallback 到
# 不可达的 Ollama 而卡死（120s 超时）；测试环境必须走可用的 OpenRouter 免费模型。
os.environ.setdefault("CLASSIFIER_MODEL", "nvidia/nemotron-3.5-content-safety:free")
os.environ.setdefault("REWRITE_MODEL", "nvidia/nemotron-3.5-content-safety:free")
os.environ.setdefault("RELEVANCE_MODEL", "nvidia/nemotron-3.5-content-safety:free")
# 本冒烟聚焦 MCP 链路：关闭提示词重写/相关性校验（content-safety 模型对简单输入常返回
# 空 content，会被 chat_with_fallback 视为失败并 fallback 不可达 Ollama 而卡 120s）。
os.environ["PROMPT_REWRITE"] = "false"
os.environ["INPUT_RELEVANCE_CHECK"] = "false"

import app.db as db
from app.db import init_db
from app.api.auth import seed_users
from app import daos, trace as trace_mod
from app.orchestrator import run_ticket, resume_ticket
from app.skills import kb as _kb, user_dir as _ud, grant as _gr  # noqa: F401 注册本地工具
from app.agents_tools import build_mcp_tools
from app.mcp.config import load_config


def _admin_id():
    with db.session() as conn:
        return conn.execute("SELECT id FROM users WHERE username='admin'").fetchone()["id"]


def _tool_calls(tid):
    with db.session() as conn:
        return [dict(x) for x in conn.execute(
            "SELECT id, tool_name, status FROM tool_calls WHERE ticket_id=? ORDER BY id",
            (tid,)).fetchall()]


def _mcp_tool_calls(tid):
    return [t for t in _tool_calls(tid) if t["tool_name"].startswith("cmdb__")]


def _traced_source(tid, source):
    return [tr for tr in trace_mod.list_for_ticket(tid) if tr["context_source"] == source]


def _reset_cmdb():
    """删除测试 CMDB 数据文件（首跑会由 server 自动 seed）。"""
    p = os.environ["CMDB_DATA"]
    if os.path.exists(p):
        os.remove(p)


def _make_mcp_high_risk_approval(admin_id, attempts: int = 3):
    """建高风险 MCP 变更工单并触发 HITL。

    真模型输出有随机性（可能本轮不调 update_asset_owner 直接收敛），最多尝试
    attempts 次、每次换一台主机/负责人，直到拿到 awaiting_approval。
    返回 (ticket_id, result, hostname)。
    """
    hosts = ["db-01", "web-02"]
    for attempt in range(attempts):
        host = hosts[attempt % len(hosts)]
        tid = daos.create_ticket(
            1, admin_id, f"变更 {host} 资产负责人",
            f"请立即调用工具 cmdb__update_asset_owner 执行变更："
            f"hostname={host}，new_owner=测试员{attempt}A，reason=项目交接。"
            f"注意：不要只描述方案，必须实际调用该工具（系统会先审批再执行）。",
            risk_level="high", intent_type="change")
        r = run_ticket(tid, actor="admin")
        if r["status"] == "awaiting_approval":
            return tid, r, host
        print(f"  第 {attempt + 1} 次未触发 HITL（{r['status']}），换主机重试…")
        # 清理本次未触发审批的失败票（保持 DB 干净可重复跑）
        try:
            daos.transition(tid, "cancelled", "admin", reason="冒烟重试清理")
        except Exception:
            pass
    raise AssertionError("多次尝试均未触发 HITL")


def main():
    init_db()
    seed_users()
    admin_id = _admin_id()
    _reset_cmdb()

    # ---- 0) 配置映射表 ----
    cfg = load_config().get("cmdb")
    assert cfg is not None, "mcp_servers.json 缺少 cmdb server 配置"
    print("[0] cmdb 映射表:", {n: m.risk for n, m in cfg.tools.items()})

    # ---- 1) 装配与白名单 ----
    emp_tools = {t.name for t in build_mcp_tools("employee", ["cmdb__query_asset",
                                                              "cmdb__update_asset_owner"])}
    op_tools = {t.name for t in build_mcp_tools("operator", ["cmdb__query_asset",
                                                             "cmdb__list_network_devices",
                                                             "cmdb__update_asset_owner"])}
    print("[1] employee 装配:", sorted(emp_tools), "| operator 装配:", sorted(op_tools))
    assert emp_tools == {"cmdb__query_asset"}, \
        f"employee 装配错误（高风险应被角色过滤）: {emp_tools}"
    assert op_tools == {"cmdb__query_asset", "cmdb__list_network_devices",
                        "cmdb__update_asset_owner"}, f"operator 装配错误: {op_tools}"

    # 未映射的工具（即使 server 暴露）不装配
    unknown = {t.name for t in build_mcp_tools("operator", ["cmdb__drop_database"])}
    assert unknown == set(), f"未映射工具被装配: {unknown}"

    # ---- 2) 低风险只读工单：CMDB 查询 ----
    tid = daos.create_ticket(1, admin_id, "查 web-01 服务器的负责人",
                             "请调用 CMDB 工具 cmdb__query_asset（参数 hostname=web-01），"
                             "查询这台服务器的负责人是谁、在哪个机房",
                             risk_level="low", intent_type="knowledge")
    r = run_ticket(tid, actor="admin")
    t = daos.get_ticket(tid)
    print(f"[场景2] id={tid} result={r['status']} status={t['status']}")
    assert r["status"] == "done" and t["status"] == "pending_confirm", r
    mcp_calls = _mcp_tool_calls(tid)
    print("  MCP 工具调用:", mcp_calls)
    assert any(c["tool_name"] == "cmdb__query_asset" for c in mcp_calls), \
        f"未出现 cmdb__query_asset 调用: {mcp_calls}"
    mcp_traces = _traced_source(tid, "mcp")
    assert mcp_traces, "无 context_source=mcp 的 trace 记录"
    print(f"  trace: {len(trace_mod.list_for_ticket(tid))} 条（其中 mcp {len(mcp_traces)} 条）")

    # ---- 3) 高风险变更：CMDB 负责人更新 → HITL 审批恢复 ----
    tid3, r3, host3 = _make_mcp_high_risk_approval(admin_id)
    t3 = daos.get_ticket(tid3)
    print(f"[场景3] id={tid3} host={host3} result={r3}")
    assert r3["status"] == "awaiting_approval", f"未触发 HITL: {r3}"
    assert t3["status"] == "awaiting_approval", t3
    aid = r3["approvals"][0]["approval_id"]
    ap = dict(daos.get_approval(aid))
    print("  审批单:", {k: ap[k] for k in ("id", "tool_name", "status")})
    assert ap["tool_name"] == "cmdb__update_asset_owner", f"审批工具不符: {ap['tool_name']}"

    daos.decide_approval(aid, "approved", "operator", "同意")
    r3b = resume_ticket(tid3, aid, actor="operator")
    t3b = daos.get_ticket(tid3)
    print(f"[场景3 恢复] result={r3b}")
    assert r3b["status"] == "done", f"审批恢复失败: {r3b}"
    assert t3b["status"] == "pending_confirm", t3b
    calls3 = _mcp_tool_calls(tid3)
    print("  变更链路 MCP 调用:", calls3)
    assert any(c["tool_name"] == "cmdb__update_asset_owner" and c["status"] == "done"
               for c in calls3), f"变更未执行成功: {calls3}"

    # CMDB 数据真实变更（owner 不再等于初始 seed 值）
    with open(os.environ["CMDB_DATA"], encoding="utf-8") as f:
        cmdb = _json.load(f)
    asset = next(a for a in cmdb["assets"] if a["hostname"] == host3)
    seed_owner = next(a["owner"] for a in cmdb["assets"] if a["hostname"] == "web-01")
    assert asset["owner"] != seed_owner, f"CMDB 负责人未更新: {asset}"
    print(f"  CMDB 真实变更: {host3} owner -> {asset['owner']}")

    print("\nMCP SMOKE OK")


if __name__ == "__main__":
    main()