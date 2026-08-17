"""把现有 registry 工具包装成 OpenAI Agents SDK 的 function_tool。

治理边界（与 M2 对齐）：
- 工具实现仍走 registry（白名单/RBAC/脱敏在 skills/*.py）
- 每次工具调用保留 M2 side-effects：去重 / tool_calls 落库 /
  trace(tool 级) / ticket_events / budget.consume_step
- 高风险工具的审批交由 SDK 原生 interruptions（function_tool needs_approval=True），
  由 agents_runner 在 run 结束后持久化审批并支持恢复；工具体不再自行发起 HITL
- 与 orchestrator 的循环引用用「运行期惰性导入」规避（模块加载期不互相 import）
- SDK 不可用时 build_sdk_tools 返回空列表，orchestrator 走既有 LLMGateway 循环
"""
from __future__ import annotations

import time

from . import daos
from . import trace as trace_mod
from .budget import BudgetExceeded
from .config import settings
from .guards import fingerprint, is_duplicate
from .skills import registry
from .db import session as db_session

try:
    from agents import function_tool, RunContextWrapper
    _SDK_OK = True
except Exception:  # pragma: no cover
    function_tool = None  # type: ignore
    RunContextWrapper = None  # type: ignore
    _SDK_OK = False


def _actor_role(actor: str) -> str:
    from .orchestrator import actor_role  # 运行期导入，避免模块加载期环
    return actor_role(actor)


def _ticket_status(ticket_id) -> str:
    with db_session() as conn:
        row = conn.execute("SELECT status FROM tickets WHERE id=?", (ticket_id,)).fetchone()
    return row["status"] if row else "?"


def _prompt_version() -> str:
    """Trace 版本号单一来源（整改⑤：统一引用 agents_runner.PROMPT_VERSION）。"""
    from .agents_runner import PROMPT_VERSION  # 运行期导入，避免模块加载期环
    return PROMPT_VERSION


def _record_tool_error(ctx, tool, error, extra=None):
    """把工具侧异常记录到 RunContext，由 runner 在 SDK 外重新抛出（保持 M2 语义）。"""
    rec = {"tool": tool, "error": str(error), "type": type(error).__name__}
    if extra:
        rec.update(extra)
    ctx.setdefault("tool_error", rec)


def _run(tool: str, ctx, params: dict):
    """统一执行体：预算/去重/tool_calls/trace/events。

    说明：本函数运行在 SDK 工具调用内。SDK 对工具异常的处理行为随版本变化
    （可能吞掉转给模型），因此这里**不向 SDK 抛治理类异常**，而是记入
    RunContext，由 agents_runner 在 Runner.run 返回后按 M2 语义重新抛出。
    """
    c = getattr(ctx, "context", None) or {}
    ticket_id = c["ticket_id"]
    actor = c["actor"]
    budget = c["budget"]
    req_id = c.get("req_id", "")
    provider = c.get("provider", "openrouter")
    model = c.get("model", settings.agent_model)  # 整改③：兜底模型名改为引用配置

    tw = registry.get_tool(tool)
    if tw is None:
        _record_tool_error(ctx, tool, RuntimeError(f"未知/未注册工具: {tool}"))
        return {"status": "error", "error": f"未知/未注册工具: {tool}"}
    if _actor_role(actor) not in tw.roles:
        _record_tool_error(ctx, tool, RuntimeError(f"角色无权限使用工具 {tool}"))
        return {"status": "error", "error": f"角色无权限使用工具 {tool}"}

    try:
        budget.check()
    except BudgetExceeded as be:
        _record_tool_error(ctx, tool, be, extra={"kind": be.kind})
        return {"status": "error", "error": str(be)}

    before = _ticket_status(ticket_id)
    if is_duplicate(ticket_id, tool, params):
        _record_tool_error(ctx, tool, RuntimeError(f"检测到重复动作: {tool}"))
        return {"status": "error", "error": f"检测到重复动作: {tool}"}

    t0 = time.time()
    try:
        resp = registry.invoke(tool, params, ctx={})
        latency = int((time.time() - t0) * 1000)
        status = "done" if resp.get("ok", True) else "failed"
    except Exception as e:  # noqa: BLE001
        resp, latency, status = {"ok": False, "error": str(e)}, 0, "failed"

    key = fingerprint(f"ticket:{ticket_id}", tool, params)
    daos.record_tool_call(ticket_id, tool, params, resp.get("result", resp), status, key, latency)
    trace_mod.write(req_id, ticket_id, model=model, provider=provider,
                    prompt_version=_prompt_version(), tool_name=tool,
                    io={"params": params, "result": resp.get("result", resp)},
                    state_before=before, state_after=_ticket_status(ticket_id),
                    latency_ms=latency, context_source="tool")
    daos.add_event(ticket_id, "tool_call",
                   {"tool": tool, "params": params, "result": resp.get("result", resp)}, actor)
    budget.consume_step()
    if status == "failed":
        _record_tool_error(ctx, tool, RuntimeError(f"工具执行失败: {tool} {resp.get('error')}"))
        return {"status": "error", "error": resp.get("error")}
    return resp.get("result", resp)


# ---- SDK tool 定义（显式签名，与 registry 内签名一致） ----

def _search_kb(ctx: RunContextWrapper, query: str,
               top_k: int = settings.vector_top_k) -> dict:
    """检索企业知识库，返回匹配的 IT/运维文档片段（只读）。"""
    return _run("search_kb", ctx, {"query": query, "top_k": top_k})


def _search_kb_direct(ctx: RunContextWrapper, topic: str) -> dict:
    """直接查看企业知识库某主题条目（内部审计用）。"""
    return _run("search_kb_direct", ctx, {"topic": topic})


def _query_user_dir(ctx: RunContextWrapper, principal: str) -> dict:
    """查询用户目录中的用户信息与权限（只读，返回脱敏）。"""
    return _run("query_user_dir", ctx, {"principal": principal})


def _grant_db_readonly(ctx: RunContextWrapper, principal: str,
                       grant_type: str = "readonly") -> dict:
    """为指定用户开通数据库只读账号（高风险，需审批）。"""
    return _run("grant_db_readonly", ctx,
                {"principal": principal, "grant_type": grant_type})


def _revoke_db_readonly(ctx: RunContextWrapper, principal: str) -> dict:
    """回收数据库只读权限（补偿动作，需审批）。"""
    return _run("revoke_db_readonly", ctx, {"principal": principal})


_SDK_TOOLS = [_search_kb, _search_kb_direct, _query_user_dir,
              _grant_db_readonly, _revoke_db_readonly]

# 工具 -> 允许角色：唯一事实源在 skills/registry.py（register_tool roles=），
# 此处不再维护 _TOOL_ROLES，统一经 registry.roles_for_tool 读取（整改①）。


def _tool_meta(fn):
    """返回 {注册表工具名, 描述, 风险}（去掉包装函数前导下划线）。"""
    name = fn.__name__.lstrip("_")
    tw = registry.get_tool(name)
    return name, tw.description if tw else "", tw.risk if tw else "low"


def _wrap_tool(fn):
    name, desc, risk = _tool_meta(fn)
    return function_tool(
        fn,
        name_override=name,
        description_override=f"{desc}（{risk} 风险）",
        use_docstring_info=True,
        needs_approval=(risk == "high"),
    )


def build_agent_tools(role: str, names) -> list:
    """按角色+工具名返回过滤后的 SDK 工具；高风险工具 needs_approval=True。"""
    if not _SDK_OK:
        return []
    want = set(names)
    out = []
    for fn in _SDK_TOOLS:
        name, _, _ = _tool_meta(fn)
        if name not in want:
            continue
        if role not in registry.roles_for_tool(name):
            continue
        out.append(_wrap_tool(fn))
    return out


def build_sdk_tools(role: str) -> list:
    """按角色返回全部 SDK 工具（兼容旧入口/兜底链路）。"""
    if not _SDK_OK:
        return []
    return [_wrap_tool(fn) for fn in _SDK_TOOLS
            if role in registry.roles_for_tool(_tool_meta(fn)[0])]


def build_mcp_tools(role: str, names) -> list:
    """按角色+工具名返回 MCP FunctionTool（转发 app.mcp.bridge；MCP 工具同样过治理）。"""
    try:
        from .mcp.bridge import build_mcp_tools as _bmt
        return _bmt(role, list(names))
    except Exception:  # pragma: no cover —— MCP 集成异常不影响本地工具面
        return []
