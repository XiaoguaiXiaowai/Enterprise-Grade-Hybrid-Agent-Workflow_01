"""MCP 工具桥接：把 MCP 工具包装为受治理的 SDK FunctionTool。

治理边界（与 agents_tools._run 完全对齐）：每个 MCP 工具调用都经过
预算检查 → 去重 → MCP 调用 → tool_calls 落库 / trace(context_source="mcp") /
ticket_events → budget.consume_step。治理异常记入 RunContext，由
agents_runner 在 SDK 之外按 M2 语义重新抛出。

为什么不用 SDK 的 MCPUtil.get_function_tools 直接挂 Agent（见设计文档 §4.3）：
- 它会绕过本地预算 / 去重 / tool_calls 落库 / 脱敏（tool_calls 只进 SDK 远端 trace）；
- 而非直接挂载则可用 FunctionTool 手工构造（params_json_schema 取 MCPTool 原始
  JSON Schema，on_invoke_tool 走本文件的治理执行体），治理语义与本地工具完全一致。

工具命名：MCP 工具统一暴露为 `{server}__{tool}`（如 cmdb__query_asset），
全局唯一、Trace 可溯源（R3）。
"""
from __future__ import annotations

import json
import time
from typing import Any

from .. import daos
from .. import trace as trace_mod
from ..budget import BudgetExceeded
from ..config import settings
from ..db import session as db_session
from ..guards import fingerprint, is_duplicate
from ..skills import masker
from ..tracelog import log, log_exception
from . import servers as mcp_servers
from .config import load_config
from .validate import validate_params

try:
    from agents.tool import FunctionTool
    from agents.tool import ToolContext
    _FT_OK = True
except Exception:  # pragma: no cover
    log_exception()
    FunctionTool = None  # type: ignore
    ToolContext = None  # type: ignore
    _FT_OK = False

# 本文件内的工具名格式
_SEP = "__"


def split_tool_name(full_name: str) -> tuple[str, str] | None:
    """`cmdb__query_asset` → ('cmdb', 'query_asset')；无分隔符返回 None。"""
    if _SEP not in full_name:
        return None
    server, tool = full_name.split(_SEP, 1)
    return server, tool


def _record_tool_error(ctx, tool: str, error: Exception, extra=None) -> None:
    """把工具侧异常记入 RunContext，由 runner 在 SDK 外重新抛出（同 agents_tools 语义）。"""
    rec = {"tool": tool, "error": str(error), "type": type(error).__name__}
    if extra:
        rec.update(extra)
    ctx_r = getattr(ctx, "context", None) or {}
    ctx_r.setdefault("tool_error", rec)


def _ticket_status(ticket_id) -> str:
    with db_session() as conn:
        row = conn.execute("SELECT status FROM tickets WHERE id=?", (ticket_id,)).fetchone()
    return row["status"] if row else "?"


def _prompt_version() -> str:
    from ..agents_runner import PROMPT_VERSION  # 运行期导入避免加载期环
    return PROMPT_VERSION


def _invoke_mcp(ctx, conn_name: str, tool_name: str, params: dict[str, Any]) -> Any:
    """治理执行体（与 agents_tools._run 同构，只把执行后端换成 MCP server）。

    返回给模型的结构化结果；治理异常按既有语义吞入 RunContext（M2 约定：
    SDK 会吞工具异常，我们在 run 返回后重新抛出）。
    """
    c = getattr(ctx, "context", None) or {}
    ticket_id = c.get("ticket_id")
    actor = c.get("actor", "system")
    budget = c.get("budget")
    req_id = c.get("req_id", "")
    full_name = f"{conn_name}__{tool_name}"
    provider = c.get("provider", "openrouter")
    model = c.get("model", settings.agent_model)

    # 1) 连接（懒连接；失败 → 工具级错误，不抛治理异常）
    try:
        conn = mcp_servers.registry.get_connection(conn_name)
    except mcp_servers.McpUnavailable as e:
        log_exception()
        _record_tool_error(ctx, full_name, RuntimeError(f"MCP: {e}"))
        return {"status": "error", "error": f"MCP: {e}"}
    if not conn.healthy or tool_name not in conn.tools:
        _record_tool_error(ctx, full_name,
                           RuntimeError(f"MCP 工具不可用: {full_name} ({conn.error or '未连接'})"))
        return {"status": "error", "error": f"MCP 工具不可用: {full_name}"}

    # 1.5) 参数校验（param_schema，映射表 P1 配置）
    verrs = _check_params(conn_name, tool_name, params)
    if verrs:
        _record_tool_error(ctx, full_name, RuntimeError(f"参数校验失败: {'；'.join(verrs)}"))
        return {"status": "error", "error": f"参数校验失败: {'；'.join(verrs)}"}

    # 2) 预算 / 去重（同 _run）
    try:
        budget.check()
    except BudgetExceeded as be:
        log_exception()
        _record_tool_error(ctx, full_name, be, extra={"kind": be.kind})
        return {"status": "error", "error": str(be)}

    if is_duplicate(ticket_id, full_name, params):
        _record_tool_error(ctx, full_name, RuntimeError(f"检测到重复动作: {full_name}"))
        return {"status": "error", "error": f"检测到重复动作: {full_name}"}

    # 3) 调用 MCP 工具
    before = _ticket_status(ticket_id)
    t0 = time.time()
    try:
        call = mcp_servers.registry.call_tool(conn, tool_name, params)
        if not call.get("ok"):
            result, status = {"error": call.get("error")}, "failed"
        else:
            result = _sanitize(call.get("data"))
            # 业务级错误（server 返回 {"ok": false}）：与本地 registry 工具语义一致记为 failed
            if isinstance(result, dict) and result.get("ok") is False:
                result, status = {"error": result.get("error", "MCP 工具业务失败")}, "failed"
            else:
                status = "done"
    except Exception as e:  # noqa: BLE001
        log_exception()
        result, status = {"error": str(e)}, "failed"
    latency = int((time.time() - t0) * 1000)

    # 4) 审计落库（工具调用 / trace / 事件），与本地工具同口径
    key = fingerprint(f"ticket:{ticket_id}", full_name, params)
    daos.record_tool_call(ticket_id, full_name, params, result, status, key, latency)
    trace_mod.write(req_id, ticket_id, model=model, provider=provider,
                    prompt_version=_prompt_version(), tool_name=full_name,
                    io={"params": params, "result": result},
                    state_before=before, state_after=_ticket_status(ticket_id),
                    latency_ms=latency, context_source="mcp")
    daos.add_event(ticket_id, "tool_call",
                   {"tool": full_name, "params": params, "result": result}, actor)
    budget.consume_step()

    if status == "failed":
        _record_tool_error(ctx, full_name, RuntimeError(f"MCP 工具执行失败: {full_name} {result}"))
        return {"status": "error", "error": str(result)}
    return result


# 脱敏规则（P0 精简版）：敏感字段打码，其余原样（避免把 hostname/ip 这类有用数据打坏）
_SENSITIVE_KEYS = {"principal", "user", "username", "owner", "email", "phone",
                   "mobile", "name", "applicant"}
_SECRET_KEYS = {"password", "secret", "token", "api_key", "apikey", "credential",
                "auth", "key"}


def _sanitize(data: Any) -> Any:
    """输出脱敏：MCP 返回的敏感字段过 masker（与本地工具脱敏同源）。"""
    if isinstance(data, dict):
        out = {}
        for k, v in data.items():
            kl = str(k).lower()
            if kl in _SECRET_KEYS:
                out[k] = masker.mask_secret(str(v))
            elif kl in _SENSITIVE_KEYS and isinstance(v, str) and v:
                out[k] = masker.mask_principal(v)
            else:
                out[k] = _sanitize(v)
        return out
    if isinstance(data, list):
        return [_sanitize(v) for v in data]
    return data


# ---- FunctionTool 装配 ----

def build_mcp_tools(role: str, names: list[str]) -> list:
    """按角色+工具名返回过滤后的 MCP FunctionTool（与 agents_tools.build_agent_tools 同构）。

    - 只装配「映射表 ∩ server 实际暴露」的工具；
    - 映射表 risk=high → needs_approval=True → SDK 原生 HITL（复用现有审批/恢复链路）。
    """
    if not settings.mcp_enabled or not _FT_OK:
        return []
    out = []
    for full_name in names:
        parsed = split_tool_name(full_name)
        if parsed is None:
            continue
        server_name, tool_name = parsed
        mapping = _resolve_mapping(server_name, tool_name)
        if mapping is None:
            continue
        if role not in mapping.roles:
            continue
        fn = _make_function_tool(server_name, tool_name)
        if fn is not None:
            out.append(fn)
    return out


def _make_function_tool(server_name: str, tool_name: str):
    """构造手工 FunctionTool（params_json_schema 直接用 MCPTool 的 schema）。"""
    from agents.tool import FunctionTool as FT

    mapping = _resolve_mapping(server_name, tool_name) or {}   # 配置异常兜底
    risk = getattr(mapping, "risk", "low")

    def _description() -> str:
        base = ""
        try:
            conn = mcp_servers.registry.get_connection(server_name)
            if conn.healthy:
                base = getattr(conn.tools.get(tool_name), "description", "") or ""
        except Exception:
            log_exception()
            pass
        if not base:
            base = getattr(mapping, "description", "") or f"MCP 工具 {server_name}/{tool_name}"
        return f"{base}（MCP:{server_name}，{risk} 风险）"

    async def on_invoke(ctx, args_json: str):
        params = {}
        if args_json:
            try:
                params = json.loads(args_json)
                if not isinstance(params, dict):
                    params = {}
            except Exception:
                log_exception()
                params = {}
        return _invoke_mcp(ctx, server_name, tool_name, params)

    # params schema 尽量用 server 提供的（转换 strict 会丢附加属性，P0 不强转）
    schema = None
    try:
        conn = mcp_servers.registry.get_connection(server_name)
        if conn.healthy:
            schema = getattr(conn.tools.get(tool_name), "input_schema", None)
    except Exception:
        log_exception()
        schema = None
    if not isinstance(schema, dict):
        schema = {"type": "object", "properties": {}}

    return FT(
        name=f"{server_name}__{tool_name}",
        description=_description(),
        params_json_schema=schema,
        on_invoke_tool=on_invoke,
        strict_json_schema=False,               # MCP schema 未必 strict，P0 不强转
        needs_approval=(risk == "high"),
    )


def _resolve_mapping(server_name: str, tool_name: str):
    cfg = load_config().get(server_name)
    if cfg is None:
        return None
    return cfg.tools.get(tool_name)


def _check_params(server_name: str, tool_name: str, params: dict) -> list[str]:
    """按映射表 param_schema 校验参数；返回错误列表（空 = 通过）。"""
    mapping = _resolve_mapping(server_name, tool_name)
    if mapping is None or not mapping.param_schema:
        return []
    return validate_params(mapping.param_schema, params)


# ===================== 传统降级链路（R4）：register_mcp_proxies =====================
#
# SDK 主链路用 FunctionTool 直挂 Agent；传统 LLMGateway 循环（_plan_and_execute）
# 按「工具名 → registry」执行，因此把已映射的 MCP 工具注册为 registry 代理工具，
# 让降级链路也能用 MCP（行为一致，设计 §4.5）。
#
# 约束：
# - 只注册 risk != "high" 的工具——高风险写操作强制走 SDK 链路的原生 HITL，
#   文本降级链路不承担审批语义（R4 简化审批）。
# - 降级链路里的工具执行由《_execute_tool》完成全部治理（预算/去重/落库/trace/HITL），
#   此处 proxy fn 只需「参数校验 + 实际调用 + 脱敏」，治理不重复。


def _invoke_mcp_proxy(server_name: str, tool_name: str, params: dict) -> dict:
    """降级链路 proxy 执行体（registry.invoke 的 fn）。

    与本地 registry 工具同约定：成功返回业务 dict（已脱敏），失败返回 {"ok": False, "error"}。
    治理（预算/去重/落库/trace/HITL）由降级链路《_execute_tool》统一承担，此处不重复。
    """
    verrs = _check_params(server_name, tool_name, params)
    if verrs:
        return {"ok": False, "error": f"参数校验失败: {'；'.join(verrs)}"}
    try:
        conn = mcp_servers.registry.get_connection(server_name)
    except mcp_servers.McpUnavailable as e:
        return {"ok": False, "error": f"MCP: {e}"}
    if not conn.healthy or tool_name not in conn.tools:
        return {"ok": False, "error": f"MCP 工具不可用: {server_name}__{tool_name}"}
    call = mcp_servers.registry.call_tool(conn, tool_name, params)
    if not call.get("ok"):
        return {"ok": False, "error": call.get("error")}
    return _sanitize(call.get("data"))


def ensure_proxies_registered() -> list[str]:
    """把只读/低风险 MCP 工具注册为 registry 代理（降级链路可用）。

    幂等：已注册的工具名跳过。返回本次新增的工具名列表。
    需在 assemble 构造 tools_allowed 之前调用（orchestrator 传统链路入口）。
    """
    from ..skills import registry
    if not settings.mcp_enabled or not _FT_OK:
        return []
    registered: list[str] = []
    for server_name, cfg in load_config().items():
        for tool_name, mapping in cfg.tools.items():
            if mapping.risk == "high":
                continue  # 高风险写操作：降级链路不承接（保持 SDK 原生审批语义）
            full = f"{server_name}__{tool_name}"
            if registry.get_tool(full) is not None:
                continue
            proxy = _make_proxy(server_name, tool_name)
            registry.register_tool(name=full, risk=mapping.risk,
                                   roles=mapping.roles,
                                   description=mapping.description or f"MCP 工具 {full}")(proxy)
            registered.append(full)
    return registered


def _make_proxy(server_name: str, tool_name: str):
    """闭包构造 proxy fn，避免循环变量捕获。"""
    def proxy(**params):
        return _invoke_mcp_proxy(server_name, tool_name, params)
    return proxy


def list_mcp_tools_for_role(role: str) -> list[str]:
    """该角色可见的全部 MCP 工具名（{server}__{tool}）——装配/展示用。"""
    out = []
    for name, cfg in load_config().items():
        for tool, mapping in cfg.tools.items():
            if role in mapping.roles:
                out.append(f"{name}__{tool}")
    return out