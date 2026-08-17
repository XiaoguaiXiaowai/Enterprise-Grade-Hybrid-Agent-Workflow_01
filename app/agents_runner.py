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
import time

from . import agents_provider as ap
from . import agents_tools as at
from . import agents_trace as atrace
from .config import settings
from .context_assembler import render
from . import trace as trace_mod
from .tracelog import log, log_exception

# Trace 版本号单一常量（整改⑤）：全项目 prompt_version 统一引用此处，
# 升级提示词只需改这一处（agents_tools / agents_trace / orchestrator 均已引用）。
PROMPT_VERSION = "M4-agents"

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
                        "actor": actor, "prompt_version": PROMPT_VERSION,
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
        log_exception()
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
        prompt_version=PROMPT_VERSION,
        io={"final_output": final_output},
        state_before=(run_ctx or {}).get("state_before", ""), state_after="done",
        token_usage=getattr(result, "usage", None) or {},
        context_source="agents_sdk_final",
    )
    return {"status": "done", "final_output": final_output,
            "provider": routing["provider"], "model": routing["model"]}


def _serialize_run_state_for_db(state, run_ctx: dict) -> str:
    """把 RunState 序列化为 JSON 落库（P2-11）。

    - context 里的 budget 等不可 JSON 序列化对象替换为 null（恢复时用 context_override
      重建，序列化内容不参与恢复）
    - 其余字段（ticket_id/actor/actor_role/state_before）保持 JSON 安全
    """
    try:
        state_json = state.to_json()
        ctx_entry = state_json.get("context") or {}
        if isinstance(ctx_entry.get("context"), dict):
            ctx_entry["context"] = {
                k: (None if k == "budget" else v) for k, v in ctx_entry["context"].items()
            }
        return json.dumps(state_json, ensure_ascii=False)
    except Exception as e:  # noqa: BLE001
        log_exception()
        return ""


def _raw_item_to_json(item) -> str:
    """把 ToolApprovalItem 的原始 raw_item 序列化落库（恢复时原样重建）。

    必须保留真实 call_id 与原始 arguments 字符串——SDK 的审批决策按
    (tool_lookup_key, arguments) 指纹匹配，任何重序列化都会导致指纹不一致、
    恢复后再次触发中断。
    """
    raw = item.raw_item
    try:
        if isinstance(raw, dict):
            return json.dumps(raw, ensure_ascii=False)
        if hasattr(raw, "model_dump"):
            return json.dumps(raw.model_dump(), ensure_ascii=False)
        return json.dumps({
            "type": getattr(raw, "type", "function_call"),
            "name": item.name or "",
            "call_id": getattr(raw, "call_id", None) or getattr(raw, "id", None),
            "arguments": getattr(raw, "arguments", "{}"),
        }, ensure_ascii=False)
    except Exception as e:  # noqa: BLE001
        log_exception()
        return ""


def _handle_interruptions(ticket_id, result, routing, req_id, run_ctx, budget, entry_agent):
    """有中断：为每个 ToolApprovalItem 落库审批并保存 RunState 供恢复。

    多 Agent 下用 entry_agent（triage）作为恢复入口，RunState 内含 agent map。
    每次中断同时把 RunState 序列化落库（approvals.run_state_json），进程重启后
    审批仍可恢复（P2-11）；内存 _PENDING 仍是首选热路径。
    """
    from . import daos  # 运行期导入
    state = result.to_state()
    agent = entry_agent or result.last_agent
    approvals = []
    actor = (run_ctx or {}).get("actor", "system")
    run_state_json = _serialize_run_state_for_db(state, run_ctx)
    routing_json = json.dumps(routing, ensure_ascii=False)
    run_ctx_json = json.dumps(
        {k: v for k, v in (run_ctx or {}).items() if k != "budget"}, ensure_ascii=False)
    budget_json = json.dumps({
        "max_steps": budget.max_steps, "max_tokens": budget.max_tokens,
        "max_seconds": budget.max_seconds, "steps": budget.steps,
        "tokens": budget.tokens, "elapsed": round(budget.elapsed, 1),
    }) if budget else "{}"
    for item in result.interruptions:
        tool_name = item.name or ""
        params = _interruption_params(item)
        aid = daos.request_approval(ticket_id, tool_name, params, actor)
        if run_state_json:
            daos.save_run_state(aid, run_state_json=run_state_json,
                                routing_json=routing_json, run_ctx_json=run_ctx_json,
                                budget_json=budget_json, req_id=req_id,
                                raw_item_json=_raw_item_to_json(item))
        daos.add_event(ticket_id, "hitl",
                       {"approval_id": aid, "tool": tool_name, "params": params}, actor)
        _PENDING[aid] = {
            "agent": agent, "state": state, "item": item,
            "ticket_id": ticket_id, "tool": tool_name, "params": params,
            "routing": routing, "req_id": req_id, "run_ctx": run_ctx, "budget": budget,
        }
        approvals.append({"approval_id": aid, "tool": tool_name, "params": params})
    return {"status": "awaiting_approval", "approvals": approvals}


def _usage_tokens(usage) -> int:
    """从 SDK Usage 对象或 dict 提取 token 总数（兼容两种形态）。"""
    if not usage:
        return 0
    try:
        if isinstance(usage, dict):
            total = usage.get("total_tokens") or 0
            if total:
                return int(total)
            return int(usage.get("prompt_tokens") or 0) + int(usage.get("completion_tokens") or 0)
        total = getattr(usage, "total_tokens", 0) or 0
        if total:
            return int(total)
        return int(getattr(usage, "prompt_tokens", 0) or 0) + \
            int(getattr(usage, "completion_tokens", 0) or 0)
    except Exception:  # noqa: BLE001
        log_exception()
        return 0


def _process_result(ticket_id, result, routing, req_id, run_ctx, budget, entry_agent=None):
    run_ctx = run_ctx or {}
    # SDK 路径预算接线：token 按每次模型调用累计（步数已由工具调用计），并做时间预算终检。
    # 恢复（resume）场景复用同一 budget 对象，累计跨轮次持续有效。
    if budget is not None:
        budget.consume_tokens(_usage_tokens(getattr(result, "usage", None)))
        budget.check()
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
    log("info", "agents_runner.run_sdk", f"agent={agent}")

    try:
        result = _run(agent, user_input, run_config, run_ctx, max_turns=budget.max_steps)
        log("info", "agents_runner.run_sdk", f"result={result}")
    except Exception as e:
        log_exception()
        if routing["provider"] == "openrouter":
            try:
                fb_agent = _build_agent_group(routing, actor_role(actor),
                                              fallback_ollama=True)
                result = _run(fb_agent, user_input, run_config, run_ctx,
                              max_turns=budget.max_steps)
                routing = {**routing, "provider": "ollama",
                           "model": settings.agent_fallback_model}
            except Exception as e2:
                log_exception()
                raise SdkUnavailable(
                    f"SDK 模型调用失败: {e}; Ollama 兜底也失败: {e2}") from e2
        else:
            raise RuntimeError(f"Agent 执行失败: {e}") from e

    return _process_result(ticket_id, result, routing, req_id, run_ctx, budget, entry_agent=agent)


def resume_run(approval_id: int, decision: str, rejection_message: str | None = None):
    """审批后恢复；返回与 run_sdk 相同的结果结构，可能再次 awaiting_approval。

    - 内存热路径：_PENDING 命中（同进程），直接用保存的 RunState 恢复
    - DB 持久化路径：进程重启后从 approvals.run_state_json 反序列化恢复（P2-11）
    """
    entry = _PENDING.get(approval_id)
    if entry:
        return _resume_with_entry(entry, approval_id, decision, rejection_message)
    return _resume_from_db(approval_id, decision, rejection_message)


def _resume_with_entry(entry: dict, approval_id: int, decision: str,
                       rejection_message: str | None):
    """内存热路径：直接用中断时保存的 RunState + ToolApprovalItem 恢复。"""
    from . import daos  # 运行期导入
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
    return _attach_resume_stats(
        _process_result(ticket_id, result, routing, req_id, run_ctx, budget,
                        entry_agent=agent),
        budget, req_id)


def _attach_resume_stats(r, budget, req_id):
    """在恢复结果上附带预算统计与 req_id（orchestrator 回写工单/长期记忆用）。"""
    if isinstance(r, dict):
        r["_budget_stats"] = {
            "steps": budget.steps if budget else 0,
            "tokens": budget.tokens if budget else 0,
            "seconds": round(budget.elapsed, 1) if budget else 0,
        }
        r["_req_id"] = req_id
    return r


def _rebuild_budget(ticket_id: int, stats: dict):
    """按落库的预算快照重建 Budget（跨重启后没有原对象，剩余时间按 elapsed 回退）。"""
    from .budget import Budget
    return Budget(
        ticket_id=ticket_id,
        max_steps=int(stats.get("max_steps", settings.budget_max_steps)),
        max_tokens=int(stats.get("max_tokens", settings.budget_max_tokens)),
        max_seconds=int(stats.get("max_seconds", settings.budget_max_seconds)),
        start=time.time() - float(stats.get("elapsed", 0)),
        steps=int(stats.get("steps", 0)),
        tokens=int(stats.get("tokens", 0)),
    )


def _find_original_call_id(state, tool_name: str) -> str | None:
    """从恢复后的 RunState 输入里找同工具名 function_call 的真实 call_id。

    审批决策按 call_id 记录，若用伪造 id 则恢复 run 时 SDK 查不到批准状态会再次中断。
    """
    for item in getattr(state, "input", None) or []:
        if isinstance(item, dict):
            if item.get("type") == "function_call" and item.get("name") == tool_name:
                cid = item.get("call_id") or item.get("id")
                if cid:
                    return str(cid)
        else:
            raw = getattr(item, "raw_item", None)
            if isinstance(raw, dict):
                if raw.get("type") == "function_call" and raw.get("name") == tool_name:
                    cid = raw.get("call_id") or raw.get("id")
                    if cid:
                        return str(cid)
            elif raw is not None and getattr(raw, "type", None) == "function_call":
                if getattr(raw, "name", None) == tool_name:
                    cid = getattr(raw, "call_id", None) or getattr(raw, "id", None)
                    if cid:
                        return str(cid)
    return None


def _rebuild_approval_item(state, ap) -> "object":
    """重建 ToolApprovalItem（DB 恢复路径）。

    优先用落库的原始 raw_item_json（保留真实 call_id 与 arguments 指纹，
    保证 SDK 批准决策能匹配到恢复 run 中的同一个 tool call）；
    老数据（无 raw_item_json）回退：从 state 输入找原始 call_id + 用
    params_json 构造 arguments（可能因序列化差异导致决策不匹配，仅尽力）。
    """
    from agents.items import ToolApprovalItem
    tool_name = ap.get("tool_name") or ""
    raw = None
    if ap.get("raw_item_json"):
        try:
            raw = json.loads(ap["raw_item_json"])
        except Exception:  # noqa: BLE001
            log_exception()
            raw = None
    if not isinstance(raw, dict):
        try:
            params = json.loads(ap.get("params_json") or "{}")
        except Exception:  # noqa: BLE001
            log_exception()
            params = {}
        call_id = _find_original_call_id(state, tool_name) or f"restored-{ap['id']}"
        raw = {
            "type": "function_call",
            "call_id": call_id,
            "name": tool_name,
            "arguments": json.dumps(params, ensure_ascii=False),
        }
    agent = getattr(state, "last_agent", None)
    return ToolApprovalItem(agent=agent, raw_item=raw, tool_name=tool_name,
                            type="tool_approval_item")


def _resume_from_db(approval_id: int, decision: str,
                    rejection_message: str | None):
    """DB 持久化路径：进程重启后从 approvals 表恢复 RunState 继续 run（P2-11）。"""
    import asyncio

    from . import daos
    from .orchestrator import actor_role  # 运行期导入避免环
    ap = dict(daos.get_approval(approval_id))  # sqlite3.Row → dict（Row 无 .get）
    if not ap.get("run_state_json"):
        raise RuntimeError(f"无此待审批 run（内存与 DB 均无恢复数据）: {approval_id}")
    # 注意：审批状态由调用方先 decide（approved/rejected）再 resume，
    # 因此这里不校验 status，只要求恢复数据存在。
    try:
        state_json = json.loads(ap["run_state_json"])
        routing = json.loads(ap["routing_json"] or "{}")
        run_ctx_db = json.loads(ap["run_ctx_json"] or "{}")
        budget_db = json.loads(ap["budget_json"] or "{}")
    except Exception as e:  # noqa: BLE001
        log_exception()
        raise RuntimeError(f"审批恢复数据损坏: {e}") from e

    ticket_id = ap["ticket_id"]
    req_id = ap.get("req_id") or ""
    actor = run_ctx_db.get("actor", "system")
    budget = _rebuild_budget(ticket_id, budget_db)
    run_ctx = dict(run_ctx_db)
    run_ctx["budget"] = budget
    agent = _build_agent_group(routing, actor_role(actor))

    # 阶段1：独立事件循环反序列化（RunState.from_json 为 async API）；
    # 结束即关闭 loop，保证阶段3 的 run_sync 在线程无事件循环环境下执行。
    from agents import RunState  # 运行期导入
    state = asyncio.run(
        RunState.from_json(agent, state_json, context_override=run_ctx))
    # 阶段2：重建审批项并做决策
    item = _rebuild_approval_item(state, ap)
    if decision == "approve":
        state.approve(item)
    else:
        state.reject(item, rejection_message=rejection_message or "被审批人驳回")
    # 阶段3：同步执行恢复的 run
    max_turns = budget.max_steps if budget else 25
    run_config = _make_run_config(ticket_id, run_ctx, req_id, routing, actor)
    result = _run(agent, state, run_config, None, max_turns=max_turns)
    return _attach_resume_stats(
        _process_result(ticket_id, result, routing, req_id, run_ctx, budget,
                        entry_agent=agent),
        budget, req_id)


class SdkUnavailable(RuntimeError):
    """OpenRouter 与 Ollama 都不可用，SDK 路径无法完成；由 orchestrator 降级到传统 LLM 网关。"""
