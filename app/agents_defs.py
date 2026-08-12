"""多 Agent 编排定义（Phase C）：triage + 知识/身份/变更 子 Agent，经 SDK handoff 协作。

每个子 Agent 只持有自己职责范围内的工具，降低幻觉与越权：
- KnowledgeAgent：企业知识库检索（search_kb / search_kb_direct）
- IdentityAgent：用户目录/权限查询（query_user_dir）
- ChangeAgent：高风险变更（grant_db_readonly / revoke_db_readonly，含 needs_approval 中断）
- TriageAgent：读工单判定意图 → handoff 到对应子 Agent（协调者，无工具）

所有 Agent 共享同一 dict 上下文（ticket_id/actor/budget），可各自指定模型。
"""
from __future__ import annotations

from . import agents_tools as at

# 子 Agent 工具集（按职责拆分）
KNOWLEDGE_TOOLS = ["search_kb", "search_kb_direct"]
IDENTITY_TOOLS = ["query_user_dir"]
CHANGE_TOOLS = ["grant_db_readonly", "revoke_db_readonly"]

_SYSTEM_TRIAGE = (
    "你是企业 IT 工单系统的调度 Agent（Triage）。你的唯一任务是根据工单内容判定意图，"
    "并转交给最合适的专业 Agent 处理。不要自行调用工具或直接回答工单问题。"
    "判定规则："
    "- 知识咨询 / IT 故障排查说明 → 转交 KnowledgeAgent（transfer_to_knowledge_agent）"
    "- 查询用户信息 / 权限 / 身份 → 转交 IdentityAgent（transfer_to_identity_agent）"
    "- 开通/回收权限等高风险变更 → 转交 ChangeAgent（transfer_to_change_agent）"
)

_SYSTEM_KNOWLEDGE = (
    "你是企业 IT 知识库检索 Agent（KnowledgeAgent）。你的职责：检索企业知识库回答"
    "IT/运维类咨询与故障排查。使用 search_kb 检索；无命中时如实告知并建议补充信息。"
    "只做知识检索，不处理用户身份或权限变更。完成时输出最终结论。"
)

_SYSTEM_IDENTITY = (
    "你是企业用户目录 Agent（IdentityAgent）。你的职责：查询用户信息与权限（只读，"
    "结果已脱敏）。使用 query_user_dir。不处理权限变更或知识库检索。完成时输出结论。"
)

_SYSTEM_CHANGE = (
    "你是企业变更执行 Agent（ChangeAgent）。你的职责：执行高风险变更（如开通/回收"
    "数据库只读权限），使用 grant_db_readonly / revoke_db_readonly。"
    "注意：高风险工具必须直接调用，系统会在真正执行前暂停审批（HITL），不要只描述风险。"
    "完成时输出最终结论。"
)


def build_agent_group(role: str, model_fn):
    """构建 Agent 组，返回 (triage_agent, [子Agent...])。

    model_fn(name, default_model) -> 该 Agent 用的模型名，便于按 Agent 差异化配置/兜底。
    """
    from agents import Agent, handoff  # 运行期导入

    kb_tools = at.build_agent_tools(role, KNOWLEDGE_TOOLS)
    id_tools = at.build_agent_tools(role, IDENTITY_TOOLS)
    ch_tools = at.build_agent_tools(role, CHANGE_TOOLS)

    knowledge_agent = Agent(
        name="KnowledgeAgent",
        instructions=_SYSTEM_KNOWLEDGE,
        tools=kb_tools,
        model=model_fn("knowledge"),
    )
    identity_agent = Agent(
        name="IdentityAgent",
        instructions=_SYSTEM_IDENTITY,
        tools=id_tools,
        model=model_fn("identity"),
    )
    change_agent = Agent(
        name="ChangeAgent",
        instructions=_SYSTEM_CHANGE,
        tools=ch_tools,
        model=model_fn("change"),
    )

    triage_agent = Agent(
        name="TriageAgent",
        instructions=_SYSTEM_TRIAGE,
        handoffs=[
            handoff(knowledge_agent, tool_name_override="transfer_to_knowledge_agent"),
            handoff(identity_agent, tool_name_override="transfer_to_identity_agent"),
            handoff(change_agent, tool_name_override="transfer_to_change_agent"),
        ],
        model=model_fn("triage"),
    )
    return triage_agent, [knowledge_agent, identity_agent, change_agent]


# 供兜底/恢复时按 Agent 名重建（内存注册表持有一组 agent，而非单个）
AGENT_NAMES = ["triage", "knowledge", "identity", "change"]
