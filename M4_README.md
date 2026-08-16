# M4 —— OpenAI Agents SDK 演进（完成）

在 M3 全栈基础上，把「自研手写 LOOP 计划-执行循环」升级为 **OpenAI Agents SDK（`openai-agents`）** 驱动，并保留原有企业治理层（状态机/预算/守卫/HITL 持久化/Trace/指标）。

## 核心变化

- **编排**：`app/agents_runner.py` 用 SDK 的 `Agent + Runner.run_sync` 取代旧的手写 `_plan_and_execute`（正则解析 tool-calling）；`Runner.run_sync` 在线程池（FastAPI `def` 路由）中执行。
- **多 Agent（Phase C）**：`app/agents_defs.py` 定义 4 个 Agent —— `TriageAgent`（调度）+ 三个子 Agent，通过 SDK 原生 **handoff** 编排：
  - `KnowledgeAgent`：`search_kb` / `search_kb_direct`
  - `IdentityAgent`：`query_user_dir`
  - `ChangeAgent`：`grant_db_readonly` / `revoke_db_readonly`（高风险）
- **HITL（Phase B）**：高风险工具用 `function_tool(needs_approval=True)` → SDK 原生 **interruptions**（`RunResult.interruptions` → `ToolApprovalItem`）。审批时 `state.approve(item)` / `state.reject(item)` 后 `Runner.run_sync(agent, state)` 恢复同一 run。
- **模型（Phase A）**：`app/agents_provider.py` 自定义 `RouterProvider`（SDK `ModelProvider`），模型名 `openrouter:...` → OpenRouter，`ollama:...` → Ollama；两者都走 `OpenAIChatCompletionsModel`（OpenAI 兼容协议）。OpenRouter 失败自动切 Ollama，均不可用抛 `SdkUnavailable` → orchestrator 降级传统 `LLMGateway`（离线 reader 可演示）。
- **Trace**：`app/agents_trace.py` 用 `TracingProcessor` 把 SDK 顶层 Trace / span 转存进本地 SQLite `traces` 表，**前端 `web/app/traces/[id]` 无需改动**；可用 `trace_mod.list_for_ticket` 核对 agent/handoff/generation span。

## 新增 / 变更文件

```
app/agents_defs.py      # 多 Agent 定义（triage + handoffs）          [新增]
app/agents_runner.py    # SDK 执行器：run + interruptions 中断/恢复   [新增]
app/agents_provider.py  # RouterProvider：OpenRouter ⇄ Ollama 模型路由 [新增]
app/agents_tools.py     # registry 工具 → function_tool 包装          [新增]
app/agents_trace.py     # SDK tracing → 本地 SQLite processor        [新增]
app/orchestrator.py     # SDK 可用走 agents_runner，否则降级旧网关     [改]
app/api/tickets.py      # 审批 approve/reject 委托 SDK resume        [改]
app/model_router.py     # openrouter 为主，意图/风险路由              [改]
scripts/smoke_agents.py # 多 Agent handoff + HITL 冒烟              [新增]
```

## 依赖

```bash
pip install -r requirements.txt   # 含 openai>=1.40, openai-agents>=0.0.9
```

> 注意：环境需联网安装（沙箱内 pip 被拦时用提升权限），`openai-agents` 当前装到 `0.20.0`，以下 API 均按该版本编写（`RunConfig` 无 `max_turns`、`function_tool` 用 `name_override`、`TracingProcessor` 为 6 方法 ABC、`RunState.from_json` 为 async）。

## 模型配置（.env）

```bash
# 主用 OpenRouter ⇄ 兜底 Ollama ⇄ 离线 reader（详见 doc/模型配置清单.md）
OPENROUTER_API_KEY=sk-or-v1-...     # 你的 OpenRouter Key
OPENROUTER_BASE_URL=https://openrouter.ai/api/v1
OLLAMA_BASE_URL=http://127.0.0.1:11434/v1   # Ollama 服务器地址（可配置）
AGENT_MODEL=openrouter/free         # Agent 主用模型
AGENT_FALLBACK_MODEL=qwen2.5:7b     # Agent 兜底模型（Ollama）
CLASSIFIER_MODEL=openrouter/free    # 意图分类主用模型
CLASSIFIER_FALLBACK_MODEL=qwen2.5:7b
ROUTER_MODEL_DEFAULT=openrouter/free
ROUTER_MODEL_HIGH_RISK=openrouter/free
```

## 验证

```bash
. .venv/bin/activate
rm -f /tmp/agents_smoke.db
python scripts/smoke_agents.py      # 需可访问 openrouter.ai（真模型）
```
断言链路：
1. 低风险知识工单 → `TriageAgent` → handoff → `KnowledgeAgent` → `search_kb` → `done`
2. 高风险工单 → `TriageAgent` → handoff → `ChangeAgent` → SDK 原生中断 → `awaiting_approval` → 审批通过 → 恢复 → `done`
3. trace（`_assert_agents_traced`）确认 agent span 出现 `TriageAgent` + 对应子 Agent

离线回归（无网络/无 Ollama）：
```bash
rm -f /tmp/m2_reg.db
APP_DB_PATH=/tmp/m2_reg.db python scripts/smoke_m2.py
```
SDK 连不上 → `SdkUnavailable` → 自动降级传统 `LLMGateway`/reader，HITL 演示保留。

## 已知边界 / 演进点
- **审批跨重启恢复**（P2-11）：审批时 `RunState` 序列化落库（`approvals.run_state_json`），
  进程重启后可从 DB 反序列化恢复同一 run（`agents_runner._resume_from_db`）；内存
  `_PENDING` 仍是同进程内的热路径。
- **SDK 路径预算**（P0 整改）：token 按每次模型调用累计（`_process_result` →
  `budget.consume_tokens`），时间预算回合终检；步数由工具调用 + `max_turns` 限制。
- 子 Agent 当前继承同一配置模型；`agents_defs.model_fn` 已预留按 Agent 差异化配置模型（如 Knowledge 用轻量、Change 用强模型）的能力点。
- M3 之前的手写 `_parse_tool_calls` 在 SDK 路径已不再使用，完全由 SDK 的 handoff + function_tool 接管（传统降级链路保留，参数按工具签名规范化）。
