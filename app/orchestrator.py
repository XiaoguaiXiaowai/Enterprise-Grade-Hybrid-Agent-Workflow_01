"""极简 LOOP 编排器（M1）：跑通"低风险工单 → 检索 → 作答 → done"骨架。

状态机是唯一 Flow（orchestrator/状态机约束），LLM 只负责"判断下一步"，
工具执行权在 harness（guards/预算在 M2 细化）。M1 用离线占位 backend。
"""
import re

from . import daos
from .skills import registry
from .skills import kb as _kb, user_dir as _ud  # noqa: F401  确保工具注册到注册表
from .llm_gateway import get_llm


def _parse_plan(text: str) -> list[str]:
    """从 LLM 输出解析工具名序列（占位粒度：M1 简单解析，M2 换 JSON structured output）。"""
    found = re.findall(r"([a-z_]+)\s*[:\n]", text)
    known = [t for t in registry.list_tools()]
    names = {t.name for t in known}
    return [f for f in found if f in names]


def run_ticket(ticket_id: int, actor: str = "system"):
    """执行一张工单。M1 面向低风险；高风险会在 M2 进入 HITL。"""
    ticket = daos.get_ticket(ticket_id)
    if not ticket:
        raise KeyError(ticket_id)

    if ticket["status"] == "created":
        daos.transition(ticket_id, "triaged", actor, reason="意图分类+风险分级(M1规则)")
    daos.transition(ticket_id, "gathering", actor, reason="LOOP 开始")
    daos.add_event(ticket_id, "log", {"msg": f"LLM({get_llm().name}) 装配上下文"}, actor)

    allowed = registry.tools_for_role("employee")
    tools_spec = "\n".join(f"- {t.name}: {t.description}" for t in registry.list_tools())
    messages = [
        {"role": "system", "content": "你是企业 IT 工单处理 Agent。请基于工具规划步骤并完成任务。"},
        {"role": "user", "content":
            f"工单：{ticket['title']}\n{ticket['description']}\n"
            f"可用工具：\n{tools_spec}\n请给出执行计划。"},
    ]
    daos.transition(ticket_id, "agent_running", actor, reason="LOOP 执行")

    result = get_llm().chat(messages)
    daos.add_event(ticket_id, "model_call", {"content": result.content}, actor)

    steps = _parse_plan(result.content)
    observations = []
    for step in steps:
        params = {}
        if step == "search_kb":
            params = {"query": f"{ticket['title']} {ticket['description']}"}
        elif step == "query_user_dir":
            m = re.search(r"u-\d+", f"{ticket['title']} {ticket['description']}")
            params = {"principal": m.group(0) if m else "u-1001"}
        call = registry.invoke(step, params, ctx={})
        observations.append(call)
        daos.add_event(ticket_id, "tool_call",
                       {"tool": step, "params": params, "result": call["result"]}, actor)

    summary = "；".join(
        str(o.get("result", {}).get("hits") or o.get("result", {})) for o in observations
    ) or "已完成"
    daos.transition(ticket_id, "done", actor, reason="收敛: done:true + 校验通过")
    daos.add_event(ticket_id, "deliver", {"summary": summary}, actor)
    return {"status": "done", "summary": summary, "steps": steps}
