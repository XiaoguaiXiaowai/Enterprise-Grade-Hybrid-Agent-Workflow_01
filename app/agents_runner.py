"""OpenAI Agents SDK 执行器：组装 Agent 并运行 Runner.run（OpenRouter 主 + Ollama 兜底）。

职责（仅替换 M2 的「计划-执行循环」，HITL 保持现状）：
- 用 SDK 的 Agent+Runner 取代 _plan_and_execute 的手写 tool-calling 解析
- 模型由 model_router.route() 决定：openrouter:... 走 OpenRouter，ollama:... 走本地
- OpenRouter 失败（网络/鉴权/模型 4xx/5xx）→ 自动用同 Prompt 在 Ollama 上重跑（方案 A 兜底）
- 工具函数仍来自 app.agents_tools（含 HITL/去重/budget/trace side-effects）
- 顶层一次 run 由 agents_trace.LocalTraceProcessor 转存进本地 SQLite（复用现有前端）
"""
from __future__ import annotations

from . import agents_provider as ap
from . import agents_tools as at
from . import agents_trace as atrace
from .config import settings
from .context_assembler import render
from . import trace as trace_mod

_SYSTEM = (
    "你是企业 IT 工单处理 Agent。依据上下文逐步规划并调用工具完成任务。"
    "每一步先判断是否需要工具：能直接回答就结束；需要数据就调用对应工具。"
    "完成时明确输出最终结论。禁止执行任何未在白名单中的操作；高风险操作只发起审批。"
)


def sdk_ok() -> bool:
    return ap.sdk_available() and at._SDK_OK


def _build_agent(instructions, tools, model):
    from agents import Agent  # 运行期导入
    return Agent(name="IT Ticket Agent", instructions=instructions, tools=tools, model=model)


def _run(agent, user_input, run_config, run_ctx, max_turns=25):
    from agents import Runner  # 运行期导入
    return Runner.run_sync(agent, input=user_input, context=run_ctx,
                           run_config=run_config, max_turns=max_turns)


def run_sdk(ticket_id: int, ctx: dict, actor: str, budget, routing: dict,
            req_id: str = ""):
    """执行 SDK 计划-工具循环；返回 (final_output, provider_used, model_used)。

    抛 _NeedsApproval / BudgetExceeded / RuntimeError，由 orchestrator 统一处理。
    """
    from .orchestrator import actor_role  # 运行期导入避免环
    user_input = render(ctx)
    run_ctx = {"ticket_id": ticket_id, "actor": actor,
               "actor_role": actor_role(actor), "budget": budget}

    provider = ap.build_provider()
    fallback_model = ap.build_fallback_model()
    tools = at.build_sdk_tools(actor_role(actor))

    from agents import RunConfig  # 运行期导入
    run_config = RunConfig(
        model_provider=provider,
        tracing_disabled=not atrace._SDK_OK,
        trace_metadata={"ticket_id": ticket_id, "req_id": req_id,
                        "provider": routing["provider"], "model": routing["model"],
                        "actor": actor, "prompt_version": "M4-agents",
                        "state_before": ctx["state"], "state_after": "agent_running"},
    )

    model = routing["model"]
    if routing["provider"] == "ollama" and not model.startswith("ollama:"):
        model = f"ollama:{model}"
    elif routing["provider"] == "openrouter" and not model.startswith("openrouter:"):
        model = f"openrouter:{model}"
    agent = _build_agent(_SYSTEM, tools, model)

    try:
        result = _run(agent, user_input, run_config, run_ctx, max_turns=budget.max_steps)
    except Exception as e:
        # OpenRouter 失败 → Ollama 兜底（同 agent，仅换 model 与 provider）
        if routing["provider"] == "openrouter":
            try:
                fb_agent = _build_agent(_SYSTEM, tools, fallback_model)
                result = _run(fb_agent, user_input, run_config, run_ctx,
                              max_turns=budget.max_steps)
                return (result.final_output or "", "ollama", settings.ollama_model_name)
            except Exception as e2:
                # OpenRouter 与 Ollama 都不可用 → 交给 orchestrator 走传统 LLM 网关（可离线 reader）
                raise SdkUnavailable(f"SDK 模型调用失败: {e}; Ollama 兜底也失败: {e2}") from e2
        raise RuntimeError(f"Agent 执行失败: {e}") from e

    # SDK 运行结束（工具异常可能被 SDK 吞掉）：把 RunContext 里记录的治理事件
    # 按 M2 语义在 SDK 外重新抛出，保证 HITL/预算/守卫行为与旧循环一致。
    from .orchestrator import _NeedsApproval  # 运行期导入
    pending = run_ctx.get("hitl_pending")
    if pending:
        raise _NeedsApproval(pending["approval_id"], pending["tool"], pending["params"])
    terr = run_ctx.get("tool_error")
    if terr:
        if terr.get("type") == "BudgetExceeded":
            from .budget import BudgetExceeded
            raise BudgetExceeded(terr.get("kind", "steps"), 0, 0)
        raise RuntimeError(terr["error"])

    # 补一条 final trace（含 final_output），复用现有前端展示收敛结论
    final_output = result.final_output or ""
    trace_mod.write(
        req_id=req_id, ticket_id=ticket_id,
        model=routing["model"], provider=routing["provider"],
        prompt_version="M4-agents",
        io={"final_output": final_output},
        state_before=ctx["state"], state_after="done",
        token_usage=getattr(result, "usage", None) or {},
        context_source="agents_sdk_final",
    )
    return (final_output, routing["provider"], routing["model"])



class SdkUnavailable(RuntimeError):
    """OpenRouter 与 Ollama 都不可用，SDK 路径无法完成；由 orchestrator 降级到传统 LLM 网关。"""
