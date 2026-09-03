# 多 Agent 编排设计

> 状态：现行 · 最后校验：2026-08-18 · 校验方式：符号级 + 行为实测（`scripts/smoke_agents.py`）
> 一句话：**Triage 读工单判定意图，handoff 给职责单一的子 Agent**——每个子 Agent 只持有自己职责范围内的工具，降低幻觉与越权。
> 实现：`app/agents_defs.py`（定义）+ `app/agents_runner.py`（执行/恢复）；功能面见 [01-企业级Agent-Harness.md](../01-核心功能/01-企业级Agent-Harness.md)。

---

## 1. Agent 拓扑（5 个）

```
                 TriageAgent（无工具，读工单判定意图）
                  │  handoff 分发
   ┌──────────────┼──────────────┬──────────────┐
   ▼              ▼              ▼              ▼
KnowledgeAgent  IdentityAgent  ChangeAgent   CmdbAgent
search_kb       query_user_dir grant_db_readonly  cmdb__query_asset
search_kb_direct               revoke_db_readonly  cmdb__list_network_devices
                                                    cmdb__update_asset_owner(high)
（+ github 只读 4 工具）        （+ github__create_issue high）
```

| Agent | 职责 | 工具（risk） | handoff 名 |
|---|---|---|---|
| `TriageAgent` | 意图判定与调度，无工具 | — | — |
| `KnowledgeAgent` | 企业知识库检索 | `search_kb`(low) / `search_kb_direct`(low) + `github__*` 只读 4 个(low) | `transfer_to_knowledge_agent` |
| `IdentityAgent` | 用户目录/权限查询 | `query_user_dir`(medium) | `transfer_to_identity_agent` |
| `ChangeAgent` | 高风险变更 | `grant_db_readonly`(high) / `revoke_db_readonly`(high) + `github__create_issue`(high) | `transfer_to_change_agent` |
| `CmdbAgent` | CMDB 外部工具面（M5，MCP） | `cmdb__query_asset`(low) / `cmdb__list_network_devices`(low) / `cmdb__update_asset_owner`(high) | `transfer_to_cmdb_agent` |

工具集常量：`app/agents_defs.py` 的 `KNOWLEDGE_TOOLS` / `IDENTITY_TOOLS` / `CHANGE_TOOLS` / `CMDB_TOOLS` / `GITHUB_READ_TOOLS` / `GITHUB_WRITE_TOOLS`。

## 2. 设计要点

1. **按职责最小化工具面**：子 Agent 只拿到本职责工具（`build_agent_group` 按 Agent 过滤装配），模型幻觉/越权的暴露面最小；
2. **SDK 原生 handoff**：`handoff(agent, tool_name_override=...)` 显式命名转移工具，triage 判定意图后调用转移；
3. **统一治理**：所有 Agent 共享同一 dict 上下文（ticket_id/actor/budget/req_id），工具执行全部经 `agents_tools._run` 治理链（预算/去重/落库/trace），不因 Agent 分叉而绕过；
4. **HITL 与 Agent 无关**：`risk=high` 工具（无论本地还是 MCP）触发 SDK 原生中断，审批恢复 `resume_run` 回到**同一 run 的同一 Agent 组**（run_state_json 持久化，跨进程可恢复）；
5. **降级对称**：OpenRouter 不可用时整组 Agent 用 Ollama 模型重建重跑（`agents_runner.run_sdk` fallback_ollama）；SDK 整体不可用则降级传统 `_plan_and_execute` 单 Agent 循环（orchestrator 兜底）；
6. **提示词外置**：各 Agent 系统提示词在 `app/prompts/agents.py`（`_SYSTEM_*`），改提示词不碰代码逻辑。

## 3. 验证方式

| 验证项 | 命令 | 模式 |
|---|---|---|
| handoff + 原生 HITL 全链路 | `python scripts/smoke_agents.py` | ✅ 需 OpenRouter key |
| 低风险走 KnowledgeAgent | `python scripts/run_all_tests.py`（T01 组） | 🔌 |
| 高风险走 ChangeAgent + 中断 | 同上（T03 组） | 🔌 |
| MCP 工具进 CmdbAgent | `python scripts/smoke_mcp.py` | ✅ 需 key |
| 跨进程恢复（run_state_json） | `python scripts/smoke_agents.py`（场景 3） | ✅ |
