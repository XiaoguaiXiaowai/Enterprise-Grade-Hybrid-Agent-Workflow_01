"""M2 LOOP 编排器：路由 → 装配上下文 → LLM 计划 → 工具(HITL/幂等/守卫) → 收敛 → Trace/指标。

职责边界（与笔记对应）：
- 状态机约束唯一 Flow（禁止跳步）
- LLM 只负责"判断下一步计划"；执行权在 harness（预算/守卫/HITL）
- 无真实后端时 LLM 网关自动回退离线 reader，保证可演示/可测
"""
import json
import re
import time

from . import daos, trace as trace_mod, metrics as metrics_mod
from .tracelog import trace_call
from .db import session as db_session
from .budget import Budget, BudgetExceeded
from .context_assembler import assemble, render
from .guards import is_duplicate, should_stop, fingerprint
from .llm_gateway import get_llm
from .model_router import route
from . import agents_runner
from .classifier import rewrite_content
from .config import settings as _settings
agents_runner.atrace.install()  # 幂等：把 SDK trace 重定向到本地 SQLite
from .skills import registry
from .skills import kb as _kb, user_dir as _ud, grant as _gr  # noqa: F401 注册工具
from .tracelog import enabled, log

PROMPT_VERSION = "M2-v1"


def _parse_tool_calls(plan_text: str) -> list[dict]:
    """从 LLM 计划解析 {tool, params} 列表（M2 简化解析，可换 structured output）。"""
    calls = []
    seen = set()
    known = {t.name for t in registry.list_tools()}
    # 支持两种形式：tool() 和 tool(param=value, ...)，以及裸 tool 名
    for tok in re.findall(r"([a-z_]+)\s*(?:\(\s*([^)]*)\s*\))?", plan_text):
        tool, raw = tok[0], tok[1]
        if tool not in known or tool in seen:
            continue
        params = {}
        if raw:
            for kv in re.findall(r"([a-zA-Z_]+)\s*=\s*([^,)]+)", raw):
                params[kv[0]] = kv[1].strip().strip("'\"")
        calls.append({"tool": tool, "params": params})
        seen.add(tool)
    return calls


def _route_params(ticket, tool: str, goal: str | None = None) -> dict:
    txt = f"{ticket['title']} {ticket['description']}"
    if tool in ("search_kb",):
        return {"query": goal or txt}
    if tool in ("query_user_dir", "grant_db_readonly", "revoke_db_readonly"):
        m = re.search(r"u-\d+", goal or txt)
        return {"principal": m.group(0) if m else "u-1001"}
    return {}


def _request_hitl(ticket_id, tool, params, actor) -> int:
    aid = daos.request_approval(ticket_id, tool, params, actor)
    daos.add_event(ticket_id, "hitl",
                   {"approval_id": aid, "tool": tool, "params": params}, actor)
    return aid


@trace_call("orchestrator.resume_ticket")
def resume_ticket(ticket_id: int, approval_id: int, actor: str = "system", decision="approve"):
    """HITL 审批后恢复执行。

    - SDK 路径生成的审批：委托 agents_runner 用原生 RunState 恢复/驳回，
      恢复完成（done/awaiting）后补上状态机收敛
    - 传统路径生成的审批：走旧 _run_loop 的抓取-执行恢复
    """
    if agents_runner.sdk_ok() and approval_id in agents_runner._PENDING:
        r = agents_runner.resume_run(approval_id, decision, rejection_message=None)
        if r["status"] in ("done", "awaiting_approval"):
            _finalize_after_sdk_resume(ticket_id, r, actor)
        return r
    return _run_loop(ticket_id, actor, resume_approval_id=approval_id)


def _finalize_after_sdk_resume(ticket_id, r, actor):
    """SDK resume 完成后补状态机收敛（resume_run 本身不碰状态机）。

    注意状态机不允许 awaiting_approval → done 直跳，需经 gathering → agent_running → done。
    """
    ticket = daos.get_ticket(ticket_id)
    if not ticket:
        return
    from .state_machine import can_transition
    if r["status"] == "done":
        _walk_transitions(ticket_id, ["gathering", "agent_running", "done"],
                          actor, "收敛: 审批恢复完成")
        daos.add_event(ticket_id, "deliver", {"summary": r.get("final_output", "")}, actor)
        _record_metrics(ticket_id, ticket["intent_type"], success=True)
    elif r["status"] == "awaiting_approval":
        # 恢复后又触发新的中断 → 回到 gathering 并留在 agent_running? 保持 awaiting_approval
        _walk_transitions(ticket_id, ["gathering"], actor, reason="HITL 中断(后续)")


def _walk_transitions(ticket_id, targets, actor, reason="", force_done=True):
    """按目标序列逐个路径转移（跳过非法/已处于的目标），直到停留在最后一个目标。"""
    from .state_machine import can_transition
    for to in targets:
        ticket = daos.get_ticket(ticket_id)
        if ticket["status"] == to:
            continue
        if can_transition(ticket["status"], to):
            daos.transition(ticket_id, to, actor, reason=reason)


@trace_call("orchestrator.run_ticket")
def run_ticket(ticket_id: int, actor: str = "system"):
    return _run_loop(ticket_id, actor)


@trace_call("orchestrator._run_loop")
def _run_loop(ticket_id: int, actor: str, resume_approval_id=None):
    ticket = daos.get_ticket(ticket_id)
    if not ticket:
        raise KeyError(ticket_id)

    # 状态机入口：created → triaged → gathering
    if ticket["status"] == "created":
        daos.transition(ticket_id, "triaged", actor, reason="意图分类+风险分级")
    if ticket["status"] in ("triaged", "awaiting_approval", "created"):
        daos.transition(ticket_id, "gathering", actor, reason="LOOP 开始")

    routing = route(ticket["intent_type"], ticket["risk_level"])
    llm = get_llm(routing["provider"] if routing["provider"] != "reader" else "reader")
    budget = Budget.start_for(ticket)
    req_id = trace_mod.new_req_id()

    tools_allowed = registry.tools_for_role(actor_role(actor))
    history = _load_history(ticket_id)
    memo = _load_memo(ticket["tenant_id"])

    if enabled():
        log("info", "run_ticket",f"routing={routing}")
        log("info", "run_ticket",f"budget={budget.left}")
        log("info", "run_ticket",f"tools_allowed={tools_allowed}")
        log("info", "run_ticket",f"ticket={ticket}")

    # 恢复模式：批准过的高危工具 → 直接执行
    resume_call = None
    if resume_approval_id:
        ap = daos.get_approval(resume_approval_id)
        if ap and ap["status"] == "approved":
            resume_call = {"tool": ap["tool_name"], "params": json.loads(ap["params_json"])}

    try:
        # M5 功能2：提示词重写——不直接并入原始工单内容，先用 LLM 总结归纳成目标。
        goal = None
        if _settings.prompt_rewrite:
            rw = rewrite_content(ticket["title"], ticket["description"])
            goal = rw["summary"]
            daos.add_event(ticket_id, "rewrite",
                           {"summary": rw["summary"], "keywords": rw["keywords"],
                            "source": rw["source"]}, actor)
            if enabled():
                log("info", "run_ticket", f"rewrite={rw}")

        ctx = assemble({"id": ticket["id"], "title": ticket["title"],
                        "status": ticket["status"], "risk_level": ticket["risk_level"]},
                       history, tools_allowed, budget.left, memo=memo, goal=goal)
        daos.add_event(ticket_id, "log", {"routing": routing, "request_id": req_id}, actor)

        daos.transition(ticket_id, "agent_running", actor, reason="LOOP 执行")

        if resume_call:
            _execute_tool(ticket_id, resume_call["tool"], resume_call["params"],
                          actor, budget, req_id, llm.provider, llm.name,
                          bypass_hitl=True)
        elif agents_runner.sdk_ok():
            try:
                sdk_result = agents_runner.run_sdk(
                    ticket_id, ctx, actor, budget, routing, req_id=req_id)
            except agents_runner.SdkUnavailable:
                # OpenRouter+Ollama 均不可用 → 降级传统 LLM 网关（可离线 reader，保持演示/测试）
                _plan_and_execute(ticket_id, ctx, actor, budget, llm, tools_allowed)
            else:
                if sdk_result["status"] == "awaiting_approval":
                    daos.transition(ticket_id, "awaiting_approval", actor, reason="HITL 中断(SDK)")
                    _record_metrics(ticket_id, ticket["intent_type"], human_takeover=True)
                    return {"status": "awaiting_approval",
                            "approvals": sdk_result["approvals"]}
                daos.add_event(ticket_id, "model_call",
                               {"content": sdk_result["final_output"],
                                "provider": sdk_result["provider"],
                                "model": sdk_result["model"]}, actor)
        else:
            _plan_and_execute(ticket_id, ctx, actor, budget, llm, tools_allowed)

        # 收敛：设置 done
        daos.transition(ticket_id, "done", actor, reason="收敛: done:true + 校验通过")
        daos.add_event(ticket_id, "deliver", {"summary": "工单已完成"}, actor)
        _record_metrics(ticket_id, ticket["intent_type"], success=True)
        return {"status": "done", "ticket_id": ticket_id}

    except _NeedsApproval as na:
        daos.transition(ticket_id, "awaiting_approval", actor, reason="HITL 中断")
        _record_metrics(ticket_id, ticket["intent_type"], human_takeover=True)
        return {"status": "awaiting_approval", "approval_id": na.approval_id,
                "tool": na.tool, "params": na.params}

    except BudgetExceeded as be:
        daos.transition(ticket_id, "failed", actor, reason=f"预算触顶: {be.kind}")
        _record_metrics(ticket_id, ticket["intent_type"], success=False, correct_failure=True)
        return {"status": "failed", "reason": f"预算触顶: {be.kind}"}

    except Exception as e:  # noqa: BLE001
        daos.transition(ticket_id, "failed", actor, reason=f"异常: {e}")
        _record_metrics(ticket_id, ticket["intent_type"], success=False)
        return {"status": "failed", "reason": str(e)}


class _NeedsApproval(Exception):
    def __init__(self, approval_id, tool, params):
        super().__init__("approval required")
        self.approval_id, self.tool, self.params = approval_id, tool, params


def actor_role(actor: str) -> str:
    """M2 占位：用 actor 名推断角色（真实场景改为查用户）。"""
    if actor in ("admin", "operator"):
        return actor
    return "employee"


def _load_history(ticket_id) -> list[dict]:
    rows = daos.list_events(ticket_id)
    hist = []
    for e in rows:
        try:
            payload = json.loads(e["payload_json"] or "{}")
        except Exception:
            payload = {}
        hist.append({"type": e["event_type"], "summary": payload.get("summary") or str(payload)[:80],
                     "content": str(payload)})
    return hist


def _load_memo(tenant_id) -> str:
    with db_session() as conn:
        row = conn.execute(
            "SELECT value_json FROM memory WHERE tenant_id=? AND key='preferences' ORDER BY id DESC LIMIT 1",
            (tenant_id,)).fetchone()
    return row["value_json"] if row else "（暂无长期记忆）"


@trace_call("orchestrator._plan_and_execute")
def _plan_and_execute(ticket_id, ctx, actor, budget, llm, tools_allowed):
    system = ("你是企业 IT 工单处理 Agent。依据上下文逐步规划并调用工具完成任务。"
              "调用格式：tool_name(param=value)。完成后输出 done:true。"
              "禁止执行任何未在白名单中的操作；高风险操作只发起审批。")
    messages = [{"role": "system", "content": system},
                {"role": "user", "content": render(ctx)}]

    before = ticket_status(ticket_id)
    result = llm.chat(messages)
    plan_text = result.content
    budget.consume_step(tokens=(result.usage or {}).get("total_tokens", 0))
    req_id = trace_mod.new_req_id()

    trace_mod.write(req_id, ticket_id,
                    model=result.model, provider=result.provider,
                    prompt_version=PROMPT_VERSION, io={"plan": plan_text},
                    state_before=before, state_after=ticket_status(ticket_id),
                    latency_ms=result.latency_ms, token_usage=result.usage,
                    context_source="assemble")
    daos.add_event(ticket_id, "model_call", {"content": plan_text, "provider": result.provider}, actor)

    calls = _parse_tool_calls(plan_text)
    ticket = daos.get_ticket(ticket_id)  # 需标题/描述补全参数
    goal = ctx.get("goal")
    for c in calls:
        params = c["params"]
        default_params = _route_params(ticket, c["tool"], goal=goal)
        merged = {**default_params, **params}  # LLM 显式参数优先，缺失用路由兜底
        _execute_tool(ticket_id, c["tool"], merged, actor, budget, req_id,
                      result.provider, result.model)
    # 若无工具但输出 done:true，直接收敛
    if should_stop(plan_text) and not calls:
        return


@trace_call("orchestrator._execute_tool")
def _execute_tool(ticket_id, tool, params, actor, budget, req_id, provider, model,
                  bypass_hitl=False):
    budget.check()
    tw = registry.get_tool(tool)
    if tw is None:
        # 未在白名单 → 拒绝（防幻觉调用）
        raise RuntimeError(f"未知/未注册工具: {tool}")
    if actor_role(actor) not in tw.roles:
        raise RuntimeError(f"角色无权限使用工具 {tool}")

    if tw.risk == "high" and not bypass_hitl:
        aid = _request_hitl(ticket_id, tool, params, actor)
        raise _NeedsApproval(aid, tool, params)

    before = ticket_status(ticket_id)
    if is_duplicate(ticket_id, tool, params):
        raise RuntimeError(f"检测到重复动作: {tool}")

    t0 = time.time()
    try:
        resp = registry.invoke(tool, params, ctx={})
        latency = int((time.time() - t0) * 1000)
        status = "done" if resp.get("ok") else "failed"
    except Exception as e:  # noqa: BLE001
        resp, latency, status = {"ok": False, "error": str(e)}, 0, "failed"

    key = fingerprint(f"ticket:{ticket_id}", tool, params)
    daos.record_tool_call(ticket_id, tool, params, resp.get("result", resp), status, key, latency)
    trace_mod.write(req_id, ticket_id, model=model, provider=provider,
                    prompt_version=PROMPT_VERSION, tool_name=tool,
                    io={"params": params, "result": resp.get("result", resp)},
                    state_before=before, state_after=ticket_status(ticket_id),
                    latency_ms=latency, context_source="tool")
    daos.add_event(ticket_id, "tool_call",
                   {"tool": tool, "params": params, "result": resp.get("result", resp)}, actor)
    budget.consume_step()
    if status == "failed":
        raise RuntimeError(f"工具执行失败: {tool} {resp.get('error')}")


def ticket_status(ticket_id) -> str:
    t = daos.get_ticket(ticket_id)
    return t["status"] if t else "?"


def _record_metrics(ticket_id, intent_type, success=False, correct_failure=False,
                    human_takeover=False):
    t = daos.get_ticket(ticket_id)
    cost = 0.0
    latency = 0
    try:
        metrics_mod.record(ticket_id, success=success, correct_failure=correct_failure,
                           latency_ms=latency, cost=cost, human_takeover=human_takeover)
    except Exception:
        pass
