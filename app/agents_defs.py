"""多 Agent 编排定义（Phase C）：triage + 知识/身份/变更/CMDB 子 Agent，经 SDK handoff 协作。

每个子 Agent 只持有自己职责范围内的工具，降低幻觉与越权：
- KnowledgeAgent：企业知识库检索（search_kb / search_kb_direct）
- IdentityAgent：用户目录/权限查询（query_user_dir）
- ChangeAgent：高风险变更（grant_db_readonly / revoke_db_readonly，含 needs_approval 中断）
- CmdbAgent（M5）：MCP 外部工具面——CMDB 资产查询（cmdb__query_asset /
  cmdb__list_network_devices）/ 资产负责人变更（cmdb__update_asset_owner，高风险）
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
    SYSTEM_CMDB as _SYSTEM_CMDB,
)

# 子 Agent 工具集（按职责拆分）
KNOWLEDGE_TOOLS = ["search_kb", "search_kb_direct"]
IDENTITY_TOOLS = ["query_user_dir"]
CHANGE_TOOLS = ["grant_db_readonly", "revoke_db_readonly"]
# M5：MCP 外部工具（{server}__{tool} 命名；权限/风险由 app/mcp/config.py 映射表决定）
CMDB_TOOLS = ["cmdb__query_asset", "cmdb__list_network_devices",
              "cmdb__update_asset_owner"]
# M5-P2：GitHub 公共 MCP server——只读查询挂 KnowledgeAgent（外部信息检索），
# 写操作 create_issue 挂 ChangeAgent（高风险 → HITL 审批）
GITHUB_READ_TOOLS = ["github__search_repositories", "github__list_repositories",
                     "github__get_issue", "github__get_issue_comments"]
GITHUB_WRITE_TOOLS = ["github__create_issue"]


def build_agent_group(role: str, model_fn):
    """构建 Agent 组，返回 (triage_agent, [子Agent...])。

    model_fn(name, default_model) -> 该 Agent 用的模型名，便于按 Agent 差异化配置/兜底。
    """
    from agents import Agent, handoff  # 运行期导入

    kb_tools = at.build_agent_tools(role, KNOWLEDGE_TOOLS)
    id_tools = at.build_agent_tools(role, IDENTITY_TOOLS)
    ch_tools = at.build_agent_tools(role, CHANGE_TOOLS)
    cmdb_tools = at.build_mcp_tools(role, CMDB_TOOLS)   # M5：MCP 工具同样走治理
    github_ro = at.build_mcp_tools(role, GITHUB_READ_TOOLS)     # M5-P2：GitHub 只读
    github_wo = at.build_mcp_tools(role, GITHUB_WRITE_TOOLS)    # M5-P2：GitHub 写（HITL）

    knowledge_agent = Agent(
        name="KnowledgeAgent",
        instructions=_SYSTEM_KNOWLEDGE,
        tools=kb_tools + github_ro,
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
        tools=ch_tools + github_wo,
        model=model_fn("change"),
    )
    cmdb_agent = Agent(
        name="CmdbAgent",
        instructions=_SYSTEM_CMDB,
        tools=cmdb_tools,
        model=model_fn("cmdb"),
    )

    triage_agent = Agent(
        name="TriageAgent",
        instructions=_SYSTEM_TRIAGE,
        handoffs=[
            handoff(knowledge_agent, tool_name_override="transfer_to_knowledge_agent"),
            handoff(identity_agent, tool_name_override="transfer_to_identity_agent"),
            handoff(change_agent, tool_name_override="transfer_to_change_agent"),
            handoff(cmdb_agent, tool_name_override="transfer_to_cmdb_agent"),
        ],
        model=model_fn("triage"),
    )
    return triage_agent, [knowledge_agent, identity_agent, change_agent, cmdb_agent]


# 供兜底/恢复时按 Agent 名重建（内存注册表持有一组 agent，而非单个）
AGENT_NAMES = ["triage", "knowledge", "identity", "change", "cmdb"]
