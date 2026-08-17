"""M2 LOOP 编排器：路由 → 装配上下文 → LLM 计划 → 工具(HITL/幂等/守卫) → 收敛 → Trace/指标。

职责边界（与笔记对应）：
- 状态机约束唯一 Flow（禁止跳步）
- LLM 只负责"判断下一步计划"；执行权在 harness（预算/守卫/HITL）
- 无真实后端时 LLM 网关自动回退离线 reader，保证可演示/可测
"""
import inspect
import json
import re
import time
from datetime import datetime

from . import daos, trace as trace_mod, metrics as metrics_mod
from .tracelog import trace_call, log_exception
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

# Trace 版本号单一来源：agents_runner.PROMPT_VERSION（整改⑤，删除原 M2-v1 重复常量）


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
        # 工单文本里明确给出用户编号 → 补全；否则不默认填充（由 _normalize_tool_params
        # 按签名校验缺失并给出明确错误，避免静默给 u-1001 授权）
        return {"principal": m.group(0)} if m else {}
    return {}


def _normalize_tool_params(tool: str, params: dict) -> dict:
    """传统链路参数规范化（P2-14 整改）：

    - 按工具函数签名过滤未知参数（LLM 文本解析可能生成 user_id= 等别名/多余键，
      直接透传会 TypeError 导致工单 failed）
    - 常见别名归一化：user_id → principal
    - 缺失必需参数时抛明确错误（替代原先默认 u-1001 的演示兜底）
    """
    tw = registry.get_tool(tool)
    if tw is None:
        return params
    try:
        sig = inspect.signature(tw.fn)
    except Exception:  # noqa: BLE001
        log_exception()
        return params
    # 变参工具（如 MCP 代理 proxy(**params)）：参数集合由 param_schema 约束，
    # 无需按函数签名过滤/补缺，直接透传（M5-P1）
    if any(p.kind == inspect.Parameter.VAR_KEYWORD for p in sig.parameters.values()):
        return params
    names = set(sig.parameters)
    out: dict = {}
    for k, v in params.items():
        if k in names:
            out[k] = v
        elif k == "user_id" and "principal" in names:
            out.setdefault("principal", v)  # 别名归一化，LLM 显式值优先于路由值
    required = [n for n, p in sig.parameters.items()
                if p.default is inspect.Parameter.empty
                and p.kind in (inspect.Parameter.POSITIONAL_OR_KEYWORD,
                               inspect.Parameter.KEYWORD_ONLY)
                and n not in ("ctx", "self")]
    missing = [n for n in required if n not in out]
    if missing:
        raise RuntimeError(
            f"工单未提供工具 {tool} 的必需参数 {missing}（如目标用户编号 u-1001），"
            f"请补充工单描述后重试")
    return out


def _request_hitl(ticket_id, tool, params, actor) -> int:
    aid = daos.request_approval(ticket_id, tool, params, actor)
    daos.add_event(ticket_id, "hitl",
                   {"approval_id": aid, "tool": tool, "params": params}, actor)
    return aid


@trace_call("orchestrator.resume_ticket")
def resume_ticket(ticket_id: int, approval_id: int, actor: str = "system", decision="approve"):
    """HITL 审批后恢复执行。

    - SDK 路径生成的审批（内存 _PENDING 命中，或 approvals 表有 run_state_json 落库）：
      委托 agents_runner 用原生 RunState 恢复/驳回——即使进程重启过也能从 DB 恢复
      （P2-11）；恢复完成（done/awaiting）后补上状态机收敛
    - 传统路径生成的审批（无 RunState）：走旧 _run_loop 的抓取-执行恢复
    """
    if agents_runner.sdk_ok():
        ap = daos.get_approval(approval_id)
        if approval_id in agents_runner._PENDING or (ap and ap["run_state_json"]):
            r = agents_runner.resume_run(approval_id, decision, rejection_message=None)
            if r["status"] in ("done", "awaiting_approval"):
                _finalize_after_sdk_resume(ticket_id, r, actor)
            return r
    return _run_loop(ticket_id, actor, resume_approval_id=approval_id)


def _finalize_after_sdk_resume(ticket_id, r, actor):
    """SDK resume 完成后补状态机收敛（resume_run 本身不碰状态机）。

    注意状态机不允许 awaiting_approval → pending_confirm 直跳，
    需经 gathering → agent_running → pending_confirm。
    """
    ticket = daos.get_ticket(ticket_id)
    if not ticket:
        return
    from .state_machine import can_transition
    if r["status"] == "done":
        _walk_transitions(ticket_id, ["gathering", "agent_running", "pending_confirm"],
                          actor, "收敛: 审批恢复完成")
        # 与正常 run 路径一致：最终结论进 model_call(final)，deliver 只写中文收尾
        daos.add_event(ticket_id, "model_call",
                       {"content": r.get("final_output", ""), "kind": "final",
                        "provider": r.get("provider", ""), "model": r.get("model", "")},
                       actor)
        daos.add_event(ticket_id, "deliver", {"summary": "本轮处理完成，请确认是否完成"}, actor)
        # 预算/结论回写 + 长期记忆（resume_run 在结果里附带预算统计与 req_id）
        _finalize_write(ticket_id, ticket,
                        r.get("_budget_stats") or {}, r.get("_req_id", ""),
                        r.get("final_output", ""))
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


def _finalize_round(ticket_id, ticket, budget, req_id, final_output=""):
    """回合收敛落账：预算用量/版本/结论回写工单 + 长期记忆写入（P0 整改）。

    - tickets 表上的 budget_used_*/prompt_version/trace_req_id/final_answer 列
      此前只有迁移建列、无业务写入，这里统一回写。
    - memory 表此前只有读（_load_memo）没有写，这里在每轮收敛时追加一条
      处理结论，形成跨工单长期记忆（最多保留 N 条，见 daos/memory.py）。
    """
    _finalize_write(ticket_id, ticket, {
        "steps": budget.steps, "tokens": budget.tokens,
        "seconds": round(budget.elapsed, 1),
    }, req_id, final_output)


def _finalize_write(ticket_id, ticket, stats: dict, req_id: str, final_output: str) -> None:
    """落账实现（预算统计以 dict 传入，SDK 审批恢复路径也可复用）。"""
    with db_session() as conn:
        conn.execute(
            "UPDATE tickets SET budget_used_steps=?, budget_used_tokens=?, budget_used_seconds=?,"
            " prompt_version=?, trace_req_id=?, final_answer=? WHERE id=?",
            (stats.get("steps", 0), stats.get("tokens", 0), stats.get("seconds", 0),
             agents_runner.PROMPT_VERSION, req_id,
             (final_output or "")[:2000], ticket_id))
    summary = (final_output or "本轮处理完成（无最终结论输出）")[:200]
    daos.append_memory(ticket["tenant_id"], "preferences", {
        "at": datetime.now().strftime("%Y-%m-%d %H:%M"),
        "ticket_id": ticket_id,
        "title": ticket["title"],
        "intent": ticket["intent_type"], "risk": ticket["risk_level"],
        "summary": summary,
    })


def _collect_final_answer(ticket_id) -> str:
    """从事件流取最近一次 final 结论（传统链路/兜底无 sdk_result 时用）。"""
    for e in reversed(daos.list_events(ticket_id)):
        if e["event_type"] == "model_call":
            try:
                p = json.loads(e["payload_json"] or "{}")
            except Exception:
                log_exception()
                p = {}
            if p.get("kind") == "final" and p.get("content"):
                return p["content"]
    return ""


@trace_call("orchestrator._run_loop")
def _run_loop(ticket_id: int, actor: str, resume_approval_id=None):
    ticket = daos.get_ticket(ticket_id)
    if not ticket:
        raise KeyError(ticket_id)

    # 状态机入口：created → triaged → gathering
    # 多轮对话：待确认完成（pending_confirm）/ 失败（failed）状态下用户追加提问 → 开新一轮
    if ticket["status"] == "created":
        daos.transition(ticket_id, "triaged", actor, reason="意图分类+风险分级")
    if ticket["status"] in ("triaged", "awaiting_approval", "created", "pending_confirm", "failed"):
        daos.transition(ticket_id, "gathering", actor,
                        reason="LOOP 开始（多轮追问）" if ticket["status"] in ("pending_confirm", "failed")
                        else "LOOP 开始")

    routing = route(ticket["intent_type"], ticket["risk_level"])
    llm = get_llm(routing["provider"] if routing["provider"] != "reader" else "reader")
    budget = Budget.start_for(ticket)
    req_id = trace_mod.new_req_id()

    tools_allowed = registry.tools_for_role(actor_role(actor))
    # M5-P1：把只读/低风险 MCP 工具注册为降级链路（LLMGateway 循环）可用的 registry 代理，
    # 使传统链路同样能消费 MCP 工具（SDK 链路用 FunctionTool，互不冲突）。
    try:
        from .mcp.bridge import ensure_proxies_registered
        ensure_proxies_registered()
    except Exception:  # noqa: BLE001 —— MCP 集成异常不影响主流程
        log_exception()
    tools_allowed = registry.tools_for_role(actor_role(actor))
    history = _load_history(ticket_id)
    memo = _load_memo(ticket["tenant_id"])

    if enabled():
        log("info", "run_ticket",f"routing={routing}")
        log("info", "run_ticket",f"budget={budget.left}")
        log("info", "run_ticket",f"tools_allowed={tools_allowed}")
        log("info", "run_ticket",f"history={history}")
        log("info", "run_ticket",f"memo={memo}")

    # 恢复模式：批准过的高危工具 → 直接执行
    resume_call = None
    sdk_result = None
    if resume_approval_id:
        ap = daos.get_approval(resume_approval_id)
        if ap and ap["status"] == "approved":
            resume_call = {"tool": ap["tool_name"], "params": json.loads(ap["params_json"])}
    try:
        # M5 功能2：提示词重写——建单时已重写并落"rewrite"事件，此处直接复用为目标；
        # 旧票/未重写时再现场重写一次兜底。
        goal = None
        if _settings.prompt_rewrite:
            rw = _lookup_rewrite(ticket_id)
            if rw is None:
                rw = rewrite_content(ticket["title"], ticket["description"])
                daos.add_event(ticket_id, "rewrite",
                               {"summary": rw["summary"], "keywords": rw["keywords"],
                                "source": rw["source"]}, actor)
            goal = rw["summary"]

        ctx = assemble({"id": ticket["id"], "title": ticket["title"],
                        "status": ticket["status"], "risk_level": ticket["risk_level"]},
                       history, tools_allowed, budget.left, memo=memo, goal=goal)
        daos.add_event(ticket_id, "log", {"routing": routing, "request_id": req_id}, actor)

        daos.transition(ticket_id, "agent_running", actor, reason="LOOP 执行")

        log("info", "orchestrator._run_loop", f"resume_call={resume_call}")
        log("info", "orchestrator._run_loop", f"sdk_ok={agents_runner.sdk_ok()}")
        if resume_call:
            log("info", "orchestrator._run_loop", "1")
            _execute_tool(ticket_id, resume_call["tool"], resume_call["params"],
                          actor, budget, req_id, llm.provider, llm.name,
                          bypass_hitl=True)
        elif agents_runner.sdk_ok():
            log("info", "orchestrator._run_loop", "2")
            try:
                log("info", "orchestrator._run_loop", "3")
                sdk_result = agents_runner.run_sdk(
                    ticket_id, ctx, actor, budget, routing, req_id=req_id)
                log("info", "orchestrator._run_loop", f"agents_runner.run_sdk -- sdk_result={sdk_result}")
            except agents_runner.SdkUnavailable:
                log_exception()
                # OpenRouter+Ollama 均不可用 → 降级传统 LLM 网关（可离线 reader，保持演示/测试）
                _plan_and_execute(ticket_id, ctx, actor, budget, llm, tools_allowed)
                log("info", "orchestrator._run_loop", f"except  plan_and_execute")
            else:
                log("info", "orchestrator._run_loop", "5")
                log("info", "orchestrator._run_loop", f"except else status={sdk_result['status']}")
                if sdk_result["status"] == "awaiting_approval":
                    daos.transition(ticket_id, "awaiting_approval", actor, reason="HITL 中断(SDK)")
                    _record_metrics(ticket_id, ticket["intent_type"], human_takeover=True)
                    return {"status": "awaiting_approval",
                            "approvals": sdk_result["approvals"]}
                daos.add_event(ticket_id, "model_call",
                               {"content": sdk_result["final_output"],
                                "provider": sdk_result["provider"],
                                "model": sdk_result["model"], "kind": "final"}, actor)
        else:
            log("info", "orchestrator._run_loop", "6")
            _plan_and_execute(ticket_id, ctx, actor, budget, llm, tools_allowed)
            log("info", "orchestrator._run_loop", f"else  plan_and_execute")
        
        # 收敛：回合完成 → 待客户确认完成（pending_confirm），确认后才变 done
        log("info", "orchestrator._run_loop", "7")
        final_answer = ""
        if sdk_result and sdk_result.get("final_output"):
            final_answer = sdk_result.get("final_output")
        else:
            # 传统链路 / resume 执行：从事件流取最近 final 结论
            final_answer = _collect_final_answer(ticket_id)
        daos.transition(ticket_id, "pending_confirm", actor, reason="回合完成，等待客户确认")
        daos.add_event(ticket_id, "deliver", {"summary": "本轮处理完成，请确认是否完成"}, actor)
        _finalize_round(ticket_id, ticket, budget, req_id, final_output=final_answer)
        _record_metrics(ticket_id, ticket["intent_type"], success=True)
        return {"status": "done", "ticket_id": ticket_id}

    except _NeedsApproval as na:
        log_exception()
        daos.transition(ticket_id, "awaiting_approval", actor, reason="HITL 中断")
        _record_metrics(ticket_id, ticket["intent_type"], human_takeover=True)
        return {"status": "awaiting_approval", "approval_id": na.approval_id,
                "tool": na.tool, "params": na.params}

    except BudgetExceeded as be:
        log_exception()
        daos.transition(ticket_id, "failed", actor, reason=f"预算触顶: {be.kind}")
        _record_metrics(ticket_id, ticket["intent_type"], success=False, correct_failure=True)
        return {"status": "failed", "reason": f"预算触顶: {be.kind}"}

    except Exception as e:  # noqa: BLE001
        log_exception()
        daos.transition(ticket_id, "failed", actor, reason=f"异常: {e}")
        _record_metrics(ticket_id, ticket["intent_type"], success=False)
        return {"status": "failed", "reason": str(e)}


class _NeedsApproval(Exception):
    def __init__(self, approval_id, tool, params):
        super().__init__("approval required")
        self.approval_id, self.tool, self.params = approval_id, tool, params


def actor_role(actor: str) -> str:
    """从 users 表取真实角色（P1-8 整改：替代原先按用户名硬判的占位逻辑）。

    查不到（如 actor=system、脚本直调或用户已被删除）时回退旧推断：admin/operator
    名字直通，其余一律 employee——保证演示/测试链路不因查库失败而中断。
    """
    try:
        with db_session() as conn:
            row = conn.execute(
                "SELECT role FROM users WHERE username=? AND is_active=1", (actor,)).fetchone()
        if row:
            return row["role"]
    except Exception:  # noqa: BLE001
        log_exception()
        pass
    if actor in ("admin", "operator"):
        return actor
    return "employee"


def _load_history(ticket_id) -> list[dict]:
    """加载工单事件历史供 Agent 参考（多轮对话：含 user_message 追问）。"""
    rows = daos.list_events(ticket_id)
    hist = []
    for e in rows:
        try:
            payload = json.loads(e["payload_json"] or "{}")
        except Exception:
            log_exception()
            payload = {}
        # 计划类 model_call（tool() 指令文本）不进上下文，避免污染
        if e["event_type"] == "model_call" and payload.get("kind") == "plan":
            continue
        if e["event_type"] in ("user_message", "system_message"):
            summary = payload.get("content") or ""
        elif e["event_type"] in ("deliver", "model_call"):
            summary = (payload.get("summary") or payload.get("content") or "")[:200]
        else:
            summary = payload.get("summary") or str(payload)[:80]
        hist.append({"type": e["event_type"], "summary": summary,
                     "content": str(payload)})
    return hist


def _load_memo(tenant_id) -> str:
    """加载租户长期记忆（memory 表，key='preferences'）并渲染成人类可读文本。

    记忆由工单收敛时写入（daos.append_memory），格式为 JSON 数组；
    兼容历史直接存文本的情况（原样返回）。
    """
    raw = daos.latest_memory(tenant_id, "preferences")
    if not raw:
        return "（暂无长期记忆）"
    try:
        entries = json.loads(raw)
        if isinstance(entries, list) and entries:
            lines = ["历史处理记录（长期记忆）："]
            for e in entries[-5:]:
                summary = str(e.get("summary", ""))[:120]
                lines.append(
                    f"- {e.get('at', '')} 工单#{e.get('ticket_id', '')}"
                    f"「{e.get('title', '')}」({e.get('intent', '')}/{e.get('risk', '')}): {summary}")
            return "\n".join(lines)
    except Exception:
        log_exception()
        pass
    return raw


def _lookup_rewrite(ticket_id) -> dict | None:
    """从事件流取最近一次 'rewrite' 事件（建单阶段持久化的重写结果），不存在返回 None。"""
    for e in reversed(daos.list_events(ticket_id)):
        if e["event_type"] == "rewrite":
            try:
                payload = json.loads(e["payload_json"] or "{}")
            except Exception:
                log_exception()
                payload = {}
            if payload.get("summary"):
                return payload
    return None


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
                    prompt_version=agents_runner.PROMPT_VERSION,
                    io={"plan": plan_text},
                    state_before=before, state_after=ticket_status(ticket_id),
                    latency_ms=result.latency_ms, token_usage=result.usage,
                    context_source="assemble")
    daos.add_event(ticket_id, "model_call",
                   {"content": plan_text, "provider": result.provider, "kind": "plan"}, actor)

    calls = _parse_tool_calls(plan_text)
    ticket = daos.get_ticket(ticket_id)  # 需标题/描述补全参数
    goal = ctx.get("goal")
    for c in calls:
        params = c["params"]
        default_params = _route_params(ticket, c["tool"], goal=goal)
        merged = {**default_params, **params}  # LLM 显式参数优先，缺失用路由兜底
        try:
            merged = _normalize_tool_params(c["tool"], merged)
        except RuntimeError as e:
            # 缺必需参数（如工单未提供目标用户）：跳过该工具并留痕，不静默失败整单。
            # 传统链路的文本解析无法区分"条件式"工具名（如 reader 模板里的
            # "若涉及用户/权限则 query_user_dir"）与真实调用意图。
            log_exception()
            daos.add_event(ticket_id, "log",
                           {"skipped_tool": c["tool"], "reason": str(e)}, actor)
            continue
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
        log_exception()
        resp, latency, status = {"ok": False, "error": str(e)}, 0, "failed"

    key = fingerprint(f"ticket:{ticket_id}", tool, params)
    daos.record_tool_call(ticket_id, tool, params, resp.get("result", resp), status, key, latency)
    trace_mod.write(req_id, ticket_id, model=model, provider=provider,
                    prompt_version=agents_runner.PROMPT_VERSION, tool_name=tool,
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
    try:
        latency, cost = _accumulate_metrics(ticket_id)
        metrics_mod.record(ticket_id, success=success, correct_failure=correct_failure,
                           latency_ms=latency, cost=cost, human_takeover=human_takeover)
    except Exception:
        log_exception()
        pass  # 指标失败不影响工单主流程


def _accumulate_metrics(ticket_id) -> tuple[int, float]:
    """从 traces 累计真实延迟与成本（整改④：修复原 latency=0/cost=0 写死）。

    - latency：traces.latency_ms 累加（每次模型/工具调用实测毫秒）
    - cost：token 用量 × 单价（LLM_COST_PER_1M_TOKENS 可配；默认 0=未定价，
      单价配置后即为按 token 用量的成本估算）
    """
    with db_session() as conn:
        rows = conn.execute(
            "SELECT latency_ms, token_usage_json FROM traces WHERE ticket_id=?",
            (ticket_id,)).fetchall()
    latency = sum(int(r["latency_ms"] or 0) for r in rows)
    cost = 0.0
    unit = _settings.llm_cost_per_1m_tokens
    for r in rows:
        try:
            usage = json.loads(r["token_usage_json"] or "{}")
        except Exception:
            log_exception()
            usage = {}
        tokens = usage.get("total_tokens") or (
            (usage.get("prompt_tokens") or 0) + (usage.get("completion_tokens") or 0))
        cost += (tokens or 0) * unit / 1_000_000
    return latency, round(cost, 6)
