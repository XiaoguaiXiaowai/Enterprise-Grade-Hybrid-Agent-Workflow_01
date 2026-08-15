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
from .prompts.agents import (
    SYSTEM_TRIAGE as _SYSTEM_TRIAGE,
    SYSTEM_KNOWLEDGE as _SYSTEM_KNOWLEDGE,
    SYSTEM_IDENTITY as _SYSTEM_IDENTITY,
    SYSTEM_CHANGE as _SYSTEM_CHANGE,
)

# 子 Agent 工具集（按职责拆分）
KNOWLEDGE_TOOLS = ["search_kb", "search_kb_direct"]
IDENTITY_TOOLS = ["query_user_dir"]
CHANGE_TOOLS = ["grant_db_readonly", "revoke_db_readonly"]


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
