"""OpenAI Agents SDK 执行器：组装 Agent + 运行 Runner + 原生 HITL 中断/恢复。

Phase B：
- 用 SDK 的 Agent+Runner 取代手写 tool-calling 循环
- 模型由 model_router.route() 决定；OpenRouter 失败自动切 Ollama 兜底，均不可用则抛
  SdkUnavailable（orchestrator 降级传统 LLM 网关）
- 高风险工具用 function_tool(needs_approval=True)，SDK 在调用前中断
  （RunResult.interruptions → ToolApprovalItem），我们据此落库审批并把
  RunState + Agent + 上下文快照存入内存注册表 _PENDING[approval_id]
- 审批通过/驳回后，用保存的 RunState + state.approve/reject 恢复同一 run；恢复后
  仍可能再次中断（递归处理）

线程语义：Runner.run_sync 要求当前线程无运行中的事件循环。FastAPI 这些 `def` 路由
在线程池中执行、无事件循环，因此同步 run/resume 安全。
"""
from __future__ import annotations

import json

from . import agents_provider as ap
from . import agents_tools as at
from . import agents_trace as atrace
from .config import settings
from .context_assembler import render
from . import trace as trace_mod

def sdk_ok() -> bool:
    return ap.sdk_available() and at._SDK_OK


def _build_agent_group(routing, role: str, fallback_ollama=False) -> "object":
    """构建多 Agent 组（triage+handoffs），返回 triage 入口 Agent。

    fallback_ollama=True 时所有子 Agent 的模型替换为 Ollama（兜底链路）。
    """
    from . import agents_defs as defs

    def model_fn(_name):
        if fallback_ollama:
            return ap.build_fallback_model()
        return _model_name(routing)

    triage, _ = defs.build_agent_group(role, model_fn)
    return triage


def _run(agent, input, run_config, run_ctx=None, max_turns=25):
    from agents import Runner  # 运行期导入
    # 恢复模式（input 为 RunState）时携带自身 context，不再单独传 context
    return Runner.run_sync(agent, input=input, context=run_ctx,
                           run_config=run_config, max_turns=max_turns)


# 内存：approval_id -> 待恢复的 run（进程内单服务，等价与 skills/grant.py 的内存态）
_PENDING: dict[int, dict] = {}


def _make_run_config(ticket_id, ctx, req_id, routing, actor):
    from agents import RunConfig  # 运行期导入
    return RunConfig(
        model_provider=ap.build_provider(),
        tracing_disabled=not atrace._SDK_OK,
        trace_metadata={"ticket_id": ticket_id, "req_id": req_id,
                        "provider": routing["provider"], "model": routing["model"],
                        "actor": actor, "prompt_version": "M4-agents",
                        "state_before": ctx.get("state", ""), "state_after": "agent_running"},
    )


def _model_name(routing) -> str:
    model = routing["model"]
    if routing["provider"] == "ollama" and not model.startswith("ollama:"):
        model = f"ollama:{model}"
    elif routing["provider"] == "openrouter" and not model.startswith("openrouter:"):
        model = f"openrouter:{model}"
    return model


def _interruption_params(item) -> dict:
    try:
        args = getattr(item, "arguments", None)
        if args:
            parsed = json.loads(args)
            return parsed if isinstance(parsed, dict) else {}
    except Exception:
        pass
    return {}


def _raise_governance(run_ctx):
    """工具侧吞掉的治理异常 → 按 M2 语义在 SDK 外重新抛出。"""
    terr = (run_ctx or {}).get("tool_error")
    if not terr:
        return
    if terr.get("type") == "BudgetExceeded":
        from .budget import BudgetExceeded
        raise BudgetExceeded(terr.get("kind", "steps"), 0, 0)
    raise RuntimeError(terr["error"])


def _handle_done(ticket_id, result, routing, req_id, run_ctx, budget):
    """无中断的收敛：写 final trace 并返回 done 结果。"""
    final_output = result.final_output or ""
    trace_mod.write(
        req_id=req_id, ticket_id=ticket_id,
        model=routing["model"], provider=routing["provider"],
        prompt_version="M4-agents",
        io={"final_output": final_output},
        state_before=(run_ctx or {}).get("state_before", ""), state_after="done",
        token_usage=getattr(result, "usage", None) or {},
        context_source="agents_sdk_final",
    )
    return {"status": "done", "final_output": final_output,
            "provider": routing["provider"], "model": routing["model"]}


def _handle_interruptions(ticket_id, result, routing, req_id, run_ctx, budget, entry_agent):
    """有中断：为每个 ToolApprovalItem 落库审批并保存 RunState 供恢复。

    多 Agent 下用 entry_agent（triage）作为恢复入口，RunState 内含 agent map。
    """
    from . import daos  # 运行期导入
    state = result.to_state()
    agent = entry_agent or result.last_agent
    approvals = []
    actor = (run_ctx or {}).get("actor", "system")
    for item in result.interruptions:
        tool_name = item.name or ""
        params = _interruption_params(item)
        aid = daos.request_approval(ticket_id, tool_name, params, actor)
        daos.add_event(ticket_id, "hitl",
                       {"approval_id": aid, "tool": tool_name, "params": params}, actor)
        _PENDING[aid] = {
            "agent": agent, "state": state, "item": item,
            "ticket_id": ticket_id, "tool": tool_name, "params": params,
            "routing": routing, "req_id": req_id, "run_ctx": run_ctx, "budget": budget,
        }
        approvals.append({"approval_id": aid, "tool": tool_name, "params": params})
    return {"status": "awaiting_approval", "approvals": approvals}


def _process_result(ticket_id, result, routing, req_id, run_ctx, budget, entry_agent=None):
    run_ctx = run_ctx or {}
    _raise_governance(run_ctx)
    if result.interruptions:
        return _handle_interruptions(ticket_id, result, routing, req_id, run_ctx,
                                     budget, entry_agent)
    return _handle_done(ticket_id, result, routing, req_id, run_ctx, budget)


def run_sdk(ticket_id, ctx, actor, budget, routing, req_id=""):
    """执行 SDK 计划-工具循环；遇原生中断返回 awaiting_approval（审批在内存注册表）。"""
    from .orchestrator import actor_role  # 运行期导入避免环
    user_input = render(ctx)
    run_ctx = {"ticket_id": ticket_id, "actor": actor,
               "actor_role": actor_role(actor), "budget": budget,
               "state_before": ctx["state"]}
    run_config = _make_run_config(ticket_id, ctx, req_id, routing, actor)
    agent = _build_agent_group(routing, actor_role(actor))

    try:
        result = _run(agent, user_input, run_config, run_ctx, max_turns=budget.max_steps)
    except Exception as e:
        if routing["provider"] == "openrouter":
            try:
                fb_agent = _build_agent_group(routing, actor_role(actor),
                                              fallback_ollama=True)
                result = _run(fb_agent, user_input, run_config, run_ctx,
                              max_turns=budget.max_steps)
                routing = {**routing, "provider": "ollama",
                           "model": settings.ollama_model_name}
            except Exception as e2:
                raise SdkUnavailable(
                    f"SDK 模型调用失败: {e}; Ollama 兜底也失败: {e2}") from e2
        else:
            raise RuntimeError(f"Agent 执行失败: {e}") from e

    return _process_result(ticket_id, result, routing, req_id, run_ctx, budget, entry_agent=agent)


def resume_run(approval_id: int, decision: str, rejection_message: str | None = None):
    """审批后恢复；返回与 run_sdk 相同的结果结构，可能再次 awaiting_approval。"""
    from . import daos  # 运行期导入
    entry = _PENDING.get(approval_id)
    if not entry:
        raise RuntimeError(f"无此待审批 run（进程重启需重新触发工单）: {approval_id}")

    state = entry["state"]
    if decision == "approve":
        state.approve(entry["item"])
    else:
        state.reject(entry["item"], rejection_message=rejection_message or "被审批人驳回")

    agent = entry["agent"]
    ticket_id = entry["ticket_id"]
    routing = entry["routing"]
    req_id = entry["req_id"]
    run_ctx = entry["run_ctx"]
    budget = entry["budget"]
    max_turns = budget.max_steps if budget else 25

    run_config = _make_run_config(ticket_id, run_ctx or {}, req_id, routing,
                                  (run_ctx or {}).get("actor", "system"))
    _PENDING.pop(approval_id, None)
    result = _run(agent, state, run_config, None, max_turns=max_turns)
    return _process_result(ticket_id, result, routing, req_id, run_ctx, budget,
                           entry_agent=agent)


class SdkUnavailable(RuntimeError):
    """OpenRouter 与 Ollama 都不可用，SDK 路径无法完成；由 orchestrator 降级到传统 LLM 网关。"""
