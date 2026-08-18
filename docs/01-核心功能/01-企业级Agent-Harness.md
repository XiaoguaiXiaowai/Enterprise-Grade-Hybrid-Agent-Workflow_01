# 企业级 Agent Harness 内核——可靠、可控、可审计

> 状态：现行 · 最后校验：2026-08-18 · 校验方式：符号级 + 行为实测
> 一句话：这是一个**企业 IT 工单智能处理系统**的内核，它演示的不是「AI 多聪明」，而是「**AI 如何被可靠、可控、可审计地接进企业业务**」——大模型只负责判断，执行权交给确定性的状态机、预算、守卫、工具白名单与人工审批。
> 代码入口：`app/orchestrator.py`（编排）· `app/state_machine.py`（状态机）· `app/taskqueue.py`（后台任务化）· `app/agents_runner.py`（SDK 执行器）· `app/skills/registry.py`（工具白名单）· `app/api/*.py`（治理 API）。

---

## 1. 定位与设计原则

系统分两层，边界清晰：

| 层 | 内容 | 归属 |
|---|---|---|
| **治理层（Harness，自研）** | 状态机、三重预算、守卫、审批持久化、Trace、指标、工具白名单/脱敏、RBAC | 可靠 / 可控 / 可审计的**全部落地**，逐行可讲 |
| **智能层（OpenAI Agents SDK）** | `Agent + Runner.run_sync` 的「计划 → 工具调用」循环、多 Agent handoff、原生 HITL 中断 | SDK 成熟且标准的能力，不重复造轮子 |

**三个关键词的落点**：

- **可靠**：状态机禁止跳步（`app/state_machine.py`）+ 乐观锁防竞态（`app/daos/tickets.py:transition`）+ 三重预算触顶终止（`app/budget.py`）+ 幂等去重（`app/guards.py`）+ 后台任务化防 HTTP 线程打满（`app/taskqueue.py`）。
- **可控**：三角色 RBAC（`app/deps.py:require_role`）+ 工具硬编码白名单与角色过滤（`app/skills/registry.py`）+ 高风险操作 HITL 审批（`app/agents_tools.py` 的 `needs_approval`）+ 输入相关性校验与内容重写（`app/classifier.py`）。
- **可审计**：`ticket_events` 事件流 + `traces` 全链路回放（`app/trace.py`）+ SDK span 转存（`app/agents_trace.py:LocalTraceProcessor`）+ 每日指标（`app/metrics.py`）+ 函数级调用日志（`app/tracelog.py`）。

**README「8 大能力」→ 本文档锚点**（完整图见仓库根 `README.md`；逐条代码映射见 [能力-代码映射表.md](06-能力-代码映射表.md)）：

| README 8 大能力 | 本文档章节 | 主要代码入口 |
|---|---|---|
| ① 流程定义 | §3 状态机与收敛 | `app/state_machine.py` `app/daos/tickets.py` |
| ② 业务入口 | §2 入口与治理 | `app/api/auth.py` `app/security.py` `app/classifier.py` |
| ③ 核心 LOOP | §4 编排与执行 · §5 上下文与路由 | `app/orchestrator.py` `app/agents_runner.py` `app/agents_defs.py` |
| ④ 业务出口 | §3 收敛闭环 · §7 HITL | `app/api/tickets.py` `app/daos/approvals.py` |
| ⑤ 工具层 MPC-Skill | §6 治理四件套 | `app/skills/*.py` `app/agents_tools.py` |
| ⑥ Trace | §8 记忆与数据 | `app/trace.py` `app/agents_trace.py` |
| ⑦ 监控评估 | §8 · §9 | `app/metrics.py` `app/api/governance.py` |
| ⑧ 预算 | §6 治理四件套 | `app/budget.py` |

**验证方式**：🔌 `.venv/bin/python -c "from app.main import app; print('路由数:', len(app.routes))"`（离线导入应用，确认入口可装配）。

---

## 2. 入口与治理：鉴权 / 会话 / 租户 / RBAC + 输入安全

### 2.1 三角色与登录会话

演示环境播种三种角色（`app/api/auth.py:SEED_ROLES`，`APP_ENV=production` 时禁用播种，见 [配置清单.md](../03-部署运维/配置清单.md)）：

| 角色 | 说明 | 可见工具（工具层 RBAC 生效） |
|---|---|---|
| `employee` | 普通员工，建单/查询/确认/追问 | 只读：`search_kb` `search_kb_direct` `query_user_dir` + 低风险 MCP（`cmdb__*` 只读、`github__*` 只读） |
| `operator` | 运维，可执行变更与审批 | 全部（含高风险变更工具） |
| `admin` | 管理员，变更/审批/删除 | 全部 |

- **密码哈希**：PBKDF2-SHA256（`app/security.py:hash_password` / `verify_password`，迭代次数 `PBKDF2_ITERATIONS` 默认 100000），存储格式 `salt$digest`。
- **会话**：`create_session` 生成随机 token（`SESSION_TOKEN_BYTES=32` 字节、`SESSION_TTL_HOURS=12` 小时），token 只以 SHA-256 哈希落 `sessions` 表；请求经 `X-Auth-Token` 头由 `app/deps.py:current_user` → `resolve_token` 解析。
- **RBAC**：`require_role(*roles)` 用于审批/删除/治理端点；`current_user` 用于登录可见端点。
- **租户隔离**：`tenant_id` 贯穿 `tickets`/`approvals`/`memory` 查询（`app/daos/tickets.py:list_tickets` 等）；`employee` 只能访问自己创建的工单，`operator`/`admin` 可见租户内全部（`app/api/tickets.py:_ensure_access`）。

### 2.2 输入安全：相关性校验 → 内容重写 → 意图/风险分级

建单为**快速落单**（`app/api/tickets.py:create_ticket` 立即返回），三道 LLM 校验在首轮执行阶段由 `app/orchestrator.py:_entry_stages` 顺序完成，每步写一条 `stage` 事件供前端聊天内流式展示：

1. **相关性校验** `classifier.relevance_check`（开关 `INPUT_RELEVANCE_CHECK`，默认开）：LLM 判定与『企业 IT 工单』不相关 → 写 `system_message` 拒绝文案并置 `cancelled`（不进状态机正式流转）；LLM 不可用/模糊时按「宁放过不误拦」放行（规则兜底）。
2. **内容重写** `classifier.rewrite_content`（开关 `PROMPT_REWRITE`，默认开）：把原始标题+描述总结归纳成简洁目标，结果落 `rewrite` 事件，执行阶段复用为目标（`app/orchestrator.py:_lookup_rewrite`），避免原始啰嗦文本污染提示词。
3. **意图/风险分级** `classifier.classify`：输出 `{intent_type, risk_level}` 回写工单元数据（`app/daos/tickets.py:update_ticket_meta`），**分类结果决定后续模型路由**（见 §5）。

追加提问同样有相关性守卫：`app/classifier.py:followup_relevance_check`（`app/api/tickets.py:followup`），不相关 → 写 `reject_followup` 事件并拒绝，不跑 Agent。

> 三函数模型选择：`classifier.py` 不直接读 `CLASSIFIER_MODEL`，而是经 `RELEVANCE_MODEL → CLASSIFIER_MODEL`、`REWRITE_MODEL → CLASSIFIER_MODEL` 的 `_first_nonempty` 链（`app/config.py`）——详见 [配置清单.md](../03-部署运维/配置清单.md) 陷阱 2。

**验证方式**：🔌 `.venv/bin/python scripts/rbac_smoke.py`（三角色可见范围与删除权限断言）；🔌 服务启动后 `curl -s localhost:8000/api/auth/seed-info`（查看播种账户）；🔌 `.venv/bin/python scripts/run_all_tests.py`（T01/T06 分类与相关性用例）。

---

## 3. 状态机与收敛（含 `pending_confirm`）

状态全集与转移表定义于 `app/state_machine.py:TRANSITIONS`（校验函数 `can_transition`），持久化与乐观锁见 `app/daos/tickets.py:transition`。**完整设计详见 [状态机设计.md](../02-架构设计/状态机设计.md)**，这里给出可执行事实：

| from | to |
|---|---|
| `created` | `triaged`, `cancelled` |
| `triaged` | `gathering`, `failed`, `cancelled` |
| `gathering` | `agent_running`, `awaiting_approval`, `failed`, `cancelled` |
| `agent_running` | `gathering`, `awaiting_approval`, `pending_confirm`, `failed`, `cancelled` |
| `awaiting_approval` | `running`*, `gathering`, `cancelled`, `failed` |
| `running`* | `done`, `failed`, `cancelled`（**历史遗留状态**：M2 早期命名，当前编排统一用 `agent_running`，无代码路径转入） |
| `pending_confirm` | `done`, `gathering`, `cancelled` |
| `done` | `archived` |
| `failed` | `archived`, `gathering`, `cancelled` |
| `cancelled` | `archived` |
| `archived` | — |

**关键语义（与直觉不同，面试重点）**：

1. **`agent_running` 不能直接 `done`**：回合收敛后工单先到 `pending_confirm`（待客户确认完成），`POST /api/tickets/{id}/confirm`（`app/api/tickets.py:confirm`）确认后才 `done` 并写 `confirmed` 事件——**终态由人确认，不由模型自封**。
2. **收敛路径**：`app/orchestrator.py:_run_loop` 回合完成后 `daos.transition(ticket_id, "pending_confirm", ...)`（orchestrator.py:404）并写 `deliver` 事件「本轮处理完成，请确认是否完成」；返回值 `{"status":"done"}` 表示**回合结果**，工单状态此时为 `pending_confirm`（`scripts/run_all_tests.py` 断言 `r==done, t==pending_confirm`）。
3. **多轮对话闭环**：`pending_confirm` / `failed` 状态下 `POST /api/tickets/{id}/messages`（`app/api/tickets.py:followup`）追加提问 → 相关性校验 → 写 `user_message` 事件 → 入队新一轮执行 → `app/orchestrator.py:_run_loop`（302-307 行）转 `gathering` 开新一轮；`done`/`archived`/`awaiting_approval`/处理中状态一律 409 拒绝。
4. **审批恢复路径**：`awaiting_approval` 审批通过后不直跳，经 `gathering → agent_running → pending_confirm` 完整走一遍（`app/orchestrator.py:_finalize_after_sdk_resume`——状态机不允许 `awaiting_approval → pending_confirm` 直跳）。
5. **失败可重试**：`failed` 允许补充信息后重试（→ `gathering`）或放弃（→ `cancelled`）；预算/异常触顶 → `failed` 并计 `correct_failure`（预期内失败）。
6. **取消**：`POST /api/tickets/{id}/cancel` 仅人触发；`created`/`triaged` 可取消，`agent_running`/`running`/`done`/`archived`/`cancelled` 409；取消时未决审批一并置 `rejected`（`app/api/tickets.py:cancel`）。

**验证方式**：🔌 `.venv/bin/python scripts/run_all_tests.py`（T04/T14：非法跳步拦截、`r=done, t=pending_confirm`、取消后未决审批作废）；🔌 建单 → run → 轮询 `GET /api/tickets/{id}` → 状态 `pending_confirm` → confirm → `done`，观察 `ticket_events` 中 `state_transition` 序列。

---

## 4. 编排与执行：orchestrator → taskqueue → SDK 多 Agent → 降级链

### 4.1 编排入口（`app/orchestrator.py`）

`run_ticket(ticket_id, actor)` → `_run_loop`：`created` 状态先跑 `_entry_stages` 流水线（§2.2）→ 状态机 `created → triaged → gathering` → `route()` 选模型 → `assemble()` 装配上下文 → 转 `agent_running` → SDK 可用走 `agents_runner.run_sdk`，否则走传统 `_plan_and_execute`。`resume_ticket(ticket_id, approval_id, actor, decision)` 是审批恢复入口（SDK 路径委托 `agents_runner.resume_run`，传统路径走 `_run_loop(resume_approval_id=...)` 抓取-执行）。

### 4.2 后台任务化（`app/taskqueue.py`）

run / 追问 / 审批端点**只做校验与入队，立即返回**，Agent 循环由 worker 线程池执行（详见 [04-高并发架构.md](04-高并发架构.md)）：

- `POST /api/tickets/{id}/run` 返回 `{"status":"queued"}`（`app/api/tickets.py:run`，仅 `created` 状态可触发，否则 409）；
- worker 池 `AGENT_WORKERS`（默认 4）封顶 Agent 并发；`try_acquire(ticket_id)` 每工单**非阻塞互斥锁**防并发双跑（run 与追问、双 run、审批与 run 撞车均被 409 拦截）；
- 事件照常写 DB（`ticket_events`），前端经 WS/轮询拿增量，**前端零改动**；
- 异常兜底：worker 内执行抛异常时，状态机允许则置 `failed` 并写 `system_message`（`taskqueue.can_mark_failed`）。

### 4.3 多 Agent 编排（`app/agents_defs.py:build_agent_group`）

**5 个 Agent**（不是 4 个）：

| Agent | 职责 | 工具 | 风险 |
|---|---|---|---|
| `TriageAgent` | 读工单判定意图 → handoff 分发（无工具，协调者） | 无 | 调度 |
| `KnowledgeAgent` | 知识库检索、IT 咨询/故障 | `search_kb` `search_kb_direct` + GitHub 只读 MCP | 低 |
| `IdentityAgent` | 用户目录/权限查询 | `query_user_dir` | 中 |
| `ChangeAgent` | 高危变更（开通/回收权限、CMDB 负责人变更、GitHub 建 issue） | `grant_db_readonly` `revoke_db_readonly` + `cmdb__update_asset_owner` `github__create_issue` | 高 |
| `CmdbAgent`（M5） | CMDB 外部工具面（MCP） | `cmdb__query_asset` `cmdb__list_network_devices`（只读）`cmdb__update_asset_owner`（高风险） | 低/高 |

handoff 工具名（`agents_defs.py:81-84`）：`transfer_to_knowledge_agent` / `transfer_to_identity_agent` / `transfer_to_change_agent` / `transfer_to_cmdb_agent`。所有 Agent 共享同一 dict 上下文（`ticket_id`/`actor`/`budget`），每个 Agent 可经 `model_fn` 差异化配置模型。多 Agent 设计权衡详见 [多Agent编排.md](../02-架构设计/多Agent编排.md)。

### 4.4 降级链（三级 + 一级）

```
SDK 路径 agents_runner.run_sdk
  ├─ 未配 OPENROUTER_API_KEY → route() 直落 provider=ollama（本地可跑）
  ├─ provider=openrouter 任意异常 → 用 Ollama 重建 Agent 组重跑（fallback_ollama）
  │    └─ 再失败 → 抛 SdkUnavailable
  └─ orchestrator 捕获 SdkUnavailable → 降级传统 LLMGateway
       （llm_gateway.chat_with_fallback：OpenRouter → Ollama → 离线 reader）
```

- SDK 路径（`app/agents_runner.py:run_sdk`）：`Runner.run_sync(triage, input, max_turns=budget.max_steps)`，工具循环与 handoff 由 SDK 原生执行；
- **边界**：仅 `provider=openrouter` 的异常触发 Ollama 重建；若**未配 key 直落 `provider=ollama` 后 Ollama 也失败**，`run_sdk` 抛 `RuntimeError`（非 `SdkUnavailable`），orchestrator 只捕获 `SdkUnavailable`——该路径**无三层兜底**，异常直接上抛置 `failed`（已知边界，见配置清单陷阱 4）；
- 传统路径（`app/orchestrator.py:_plan_and_execute`）：LLM 输出计划文本 → `_parse_tool_calls` 解析 → `_execute_tool` 逐工具执行；`_ReaderBackend` 为离线占位（无密钥/离线仍可演示 HITL/Trace/指标）；
- 传统链路也消费 MCP 工具：`app/mcp/bridge.py:ensure_proxies_registered` 把低风险 MCP 工具注册进 registry 代理。

**验证方式**：🔌 `.venv/bin/python scripts/run_all_tests.py`（offline 模式：`at._SDK_OK=False` → 传统网关 reader 确定性链路）；🔌 `.venv/bin/python scripts/smoke_m2.py`；`python3 scripts/smoke_agents.py`（真实 SDK 链路：handoff + HITL + 跨进程恢复，需 OpenRouter key）。

---

## 5. 上下文装配与模型路由

### 5.1 上下文装配（`app/context_assembler.py`）

- **选**：只带最近 `CTX_MAX_HISTORY`（默认 8）条历史（`_load_history` 过滤掉 plan 类 `model_call` 与 `stage` 进度事件，防污染）；
- **压**：单条 observation 超过 `CTX_MAX_OBS_LEN`（默认 1200）时压缩；`CTX_LLM_COMPRESS`（默认关）开启时用 LLM 摘要，失败自动回退头尾截断；
- **截**：历史条数超过 `CTX_MIN_TRUNCATE`（默认 30）触发 Auto-Compact，早期历史压成一段摘要（`_auto_compact`）；
- **结构化**：固定顺序段落，防「自我污染」——`<goal><state><history><memo><tools><constraints><budget_left>`（`app/prompts/context.py:render_lines`）；**剩余预算标签是 `<budget_left>`**，`Budget.left` 注入剩余步数/Token/时间；
- **memo**：读取 `memory` 表最近一条 `preferences`（工单收敛时写入，见 §8）。

提示词全部外置：`app/prompts/agents.py`（5 个 Agent 系统提示词）· `app/prompts/classifier.py`（相关性/重写/分类提示词与规则）· `app/prompts/context.py`（上下文模板）。

### 5.2 模型路由（`app/model_router.py:route`）

返回 `{provider, model, intent, risk}`（写入 Trace 供审计）：

1. 用户显式设 `AGENT_MODEL` → 所有意图用它（OpenRouter 上可切任意模型 id）；
2. 未显式设置 → 按意图给默认（`INTENT_MODELS`：知识/查询/排障 → `ROUTER_MODEL_DEFAULT`，变更 → `ROUTER_MODEL_HIGH_RISK`）；
3. **高风险升格**：`risk_level=high` 且未显式配置时，一律升格 `ROUTER_MODEL_HIGH_RISK`；
4. 未配 `OPENROUTER_API_KEY` → 直落 `provider=ollama` + `AGENT_FALLBACK_MODEL`，保证离线可跑。

> ⚠️ 模型默认值**全部为空串**（`app/config.py`：`AGENT_MODEL` / `CLASSIFIER_MODEL` / `ROUTER_MODEL_DEFAULT` 等均 `os.getenv(..., "")`），不是旧文档的 `openrouter/free`；实际取值以 `.env` / [配置清单.md](../03-部署运维/配置清单.md) 为准。

**验证方式**：🔌 `.venv/bin/python scripts/verify_model_config.py`（配置优先级/三级熔断/键一致性断言）；🔌 服务启动后 `curl -s 'localhost:8000/api/governance/routes?intent_type=change&risk_level=high'`（需 operator/admin token）。

---

## 6. 治理四件套：预算 / 守卫 / 工具白名单与 RBAC / 脱敏

### 6.1 三重预算（`app/budget.py`）

| 预算 | 默认 | 行为 |
|---|---|---|
| 步数 `steps` | `BUDGET_MAX_STEPS=25` | SDK 路径由 `max_turns` 硬限制 + 每工具调用 `consume_step`；传统路径每次 LLM 调用计步 |
| Token `tokens` | `BUDGET_MAX_TOKENS=200000` | SDK 路径在 `_process_result` 按每次模型调用 `consume_tokens`；传统路径按 plan 调用 usage 累计 |
| 时间 `seconds` | `BUDGET_MAX_SECONDS=600` | 墙钟时间，工具调用与回合终检时 `check()` |

任何一把触顶 → `BudgetExceeded` → orchestrator 置 `failed` 并计 `correct_failure`（预期内失败，区别于模型乱来的失败）。SDK 路径的工具侧异常先记入 `RunContext.tool_error`，由 `agents_runner._raise_governance` 在 SDK 外按 M2 语义重抛。

### 6.2 守卫（`app/guards.py`）

- `fingerprint(scope, tool, params)`：参数规范化 JSON 的 sha256 指纹（`scope:tool:params`）；
- `is_duplicate`：同工单 + 同工具 + 同指纹在 `DEDUP_WINDOW_MINUTES`（默认 5 分钟）窗口内已有 `status='done'` 记录 → 拦截（时间窗用 UTC 字符串比较，与 `tool_calls.created_at` 对齐）；
- `should_stop`：计划文本含停止条件词 `done:true` → 终止（传统链路收敛信号）。

### 6.3 工具白名单与 RBAC 过滤

**注册即白名单**：工具必须经 `@register_tool(name, risk, roles)` 登记（`app/skills/registry.py`），未注册工具被调用 → 直接拒绝（防幻觉调用）。执行时 `agents_tools._run` 内**再次复查角色**（装配过滤 + 执行复查双保险）。

静态工具 5 个（`app/skills/*.py`）：

| 工具 | 风险 | roles | 说明 |
|---|---|---|---|
| `search_kb` | low | 全员 | FTS5 + LIKE 兜底混合检索（`app/skills/kb.py:search_kb`） |
| `search_kb_direct` | low | 全员 | 直接取条目（审计用） |
| `query_user_dir` | medium | 全员 | 查 `users` 表用户/权限，返回脱敏（`app/skills/user_dir.py:query_user_dir`） |
| `grant_db_readonly` | high | operator/admin | 授权落 `grants` 表（SQLite 持久化，非内存态），HITL + 幂等 + 30 天过期 |
| `revoke_db_readonly` | high | operator/admin | 回收 `grants` 表授权（真实生效），HITL |

MCP 映射工具 8 个（`mcp_servers.json`，配置即白名单：risk/roles/param_schema 由 `app/mcp/config.py` 校验；详见 [03-MCP工具集成.md](03-MCP工具集成.md)）：

| 工具 | 风险 | 说明 |
|---|---|---|
| `cmdb__query_asset` / `cmdb__list_network_devices` | low | CMDB 资产/网络设备只读查询 |
| `cmdb__update_asset_owner` | high | 资产负责人变更（HITL） |
| `github__search_repositories` / `github__list_repositories` / `github__get_issue` / `github__get_issue_comments` | low | GitHub 只读检索 |
| `github__create_issue` | high | 创建 issue（HITL） |

### 6.4 脱敏（`app/skills/masker.py`）

`mask_principal`（邮箱保留首字符+域名、姓名居中打码）、`mask_secret`（凭证打码 `***`）；工具结果在落库/trace 前打码。

**验证方式**：🔌 `.venv/bin/python scripts/run_all_tests.py`（T08 未白名单工具被拒 / T09 RBAC / T10 重复动作拦截 / T11 预算触顶）；🔌 观察 `GET /api/tickets/{id}/conversation` 与 trace 中敏感字段已打码。

---

## 7. HITL 审批闭环（含跨进程恢复）

```
高风险工具被模型调用（risk=high → function_tool(needs_approval=True)）
   ▼
SDK 原生中断：Runner 在执行前暂停，RunResult.interruptions = [ToolApprovalItem]
   ▼
agents_runner._handle_interruptions：
   ① 审批单落库（approvals 表，status=pending）
   ② RunState 序列化快照落库（approvals.run_state_json 等，供跨进程恢复）
   ③ 内存注册表 _PENDING[approval_id]（同进程热路径）
   ▼
工单转 awaiting_approval；审批台 POST /api/tickets/{id}/approve（operator/admin）
   ├─ approve → decide_approval("approved") → 入队 resume_ticket
   └─ reject  → decide_approval("rejected") → 同样恢复 run（让 Agent 得知被驳回并调整策略）
   ▼
agents_runner.resume_run：内存 _PENDING 命中 → state.approve/reject(item)
   → Runner.run_sync(agent, state) 从【同一 run】继续（含已发生的 handoff）；
   进程已重启 → _resume_from_db：从 approvals 表反序列化 RunState 恢复（P2-11）
   ▼
恢复后可能再次中断（递归处理）；收敛后经 _finalize_after_sdk_resume 补状态机路径
   （gathering → agent_running → pending_confirm）
```

关键事实：

- 审批持久化：`approvals` 表 + `run_state_json` / `routing_json` / `run_ctx_json` / `budget_json` / `req_id` / `raw_item_json` 六列（`app/daos/approvals.py:save_run_state`），`raw_item_json` 保留真实 call_id 与 arguments 指纹，保证 SDK 审批决策能匹配恢复 run 中的同一 tool call（`app/agents_runner.py:_rebuild_approval_item`）；
- **跨进程恢复**：进程重启后 `_PENDING` 丢失，`resume_run` 自动落到 `_resume_from_db`（`RunState.from_json` 为 async API，独立事件循环反序列化后再同步 `run_sync`，`scripts/smoke_agents.py` 有清空 `_PENDING` 模拟重启的断言）；
- **权限状态 SQLite 落库**：`grants` 表（`active`/`revoked`/`expired` + 过期时间，默认 30 天自动过期惰性标记），授权/回收真实生效，非进程内内存态（`app/skills/grant.py`）；
- 传统路径的 HITL：`app/orchestrator.py:_execute_tool` 对 `risk=high` 且未 bypass 的工具 `_request_hitl` 抛 `_NeedsApproval` → `awaiting_approval`（无 RunState 时走抓取-执行恢复）。

**验证方式**：🔌 `.venv/bin/python scripts/smoke_m2.py`（低风险收敛 + 高风险 HITL + 审批恢复全链路）；`python3 scripts/smoke_agents.py`（SDK 原生中断 + 跨进程恢复，需 OpenRouter key）。

---

## 8. 记忆与数据：五分层落库位置

| 层 | 含义 | 落库位置 | 读写入口 |
|---|---|---|---|
| Session | 登录会话 | `sessions` 表（token 哈希 + 过期时间） | `app/security.py:create_session` / `resolve_token` |
| Task | 任务状态 | `tickets` 表（status / risk_level / intent_type / budget_used_* / prompt_version / trace_req_id / final_answer） | `app/daos/tickets.py`（`transition` 单一写入口） |
| Memory | 长期记忆 | `memory` 表（按 tenant_id+key，每 key 保留最近 10 条） | `app/daos/memory.py:append` / `latest`；工单收敛时 `_finalize_write` 写 `preferences`，上下文经 `<memo>` 注入 |
| Artifact | 执行产物 | `tool_calls` 表（参数/结果/状态/idempotency_key/延迟/重试）+ `tickets.final_answer` | `app/daos/tool_calls.py:record`；`app/agents_tools.py:_run` / `orchestrator._execute_tool` |
| Trace | 审计链路 | `traces` 表（模型/提示词版本/上下文来源/工具/状态前后/延迟/Token）；SDK 顶层 span 经 `LocalTraceProcessor` 转存 | `app/trace.py:write` / `list_for_ticket`；`app/agents_trace.py` |

**事件流（`ticket_events` 表）**：每步操作落一条事件（`app/daos/tickets.py:add_event`）。现行事件类型：`created` `state_transition` `user_message` `system_message` `stage`（relevance/rewrite/classify 进度）`rewrite` `model_call` `tool_call` `hitl` `deliver` `log` `reject_followup` `confirmed` `closed` `cancelled`——前端经 WS（`WS /api/tickets/{id}/ws`）或轮询取增量。

**验证方式**：🔌 跑一单后 `GET /api/tickets/{id}/conversation`（事件序列）+ `GET /api/governance/traces/{ticket_id}`（Trace 回放）；🔌 `.venv/bin/python -c "import sqlite3; c=sqlite3.connect('data/app.db'); print([r[0] for r in c.execute(\"SELECT name FROM sqlite_master WHERE type='table'\")])"`（离线查看全部表）。

---

## 9. API 清单（全量）

> 权限列：公开 / 登录 / 归属（登录且租户匹配，employee 仅本人工单）/ operator+admin。认证头：`X-Auth-Token`。实时主通道为 **WebSocket**，SSE 仅兼容旧客户端。

### 9.1 认证 `app/api/auth.py`（prefix `/api/auth`）

| 方法 | 路径 | 权限 | 说明 |
|---|---|---|---|
| POST | `/api/auth/login` | 公开 | 登录，返回 `{token, user}` |
| GET | `/api/auth/me` | 登录 | 当前用户上下文 |
| GET | `/api/auth/seed-info` | 公开 | 演示播种信息（生产不返回密码） |
| POST | `/api/auth/seed` | 公开（生产禁用） | 播种演示账户（admin/operator/employee） |

### 9.2 工单 `app/api/tickets.py`（prefix `/api/tickets`）

| 方法 | 路径 | 权限 | 说明 |
|---|---|---|---|
| POST | `/api/tickets` | 登录 | 建单（快速落单，返回 TicketOut） |
| GET | `/api/tickets` | 登录 | 分页列表（status 逗号筛选 / q 关键词） |
| GET | `/api/tickets/all` | operator/admin | 全部工单（审批台/治理用） |
| GET | `/api/tickets/{id}` | 归属 | 工单详情（越权 403） |
| DELETE | `/api/tickets/{id}` | admin | 删除（级联清理事件/审批/trace/指标） |
| POST | `/api/tickets/{id}/run` | 归属 | 触发执行（仅 `created`；后台化，返回 `{"status":"queued"}`） |
| POST | `/api/tickets/{id}/messages` | 归属 | 追加提问（`pending_confirm`/`failed`；入队新一轮，返回 `{"status":"accepted"}`） |
| POST | `/api/tickets/{id}/confirm` | 归属 | 客户确认完成（`pending_confirm → done`，写 `confirmed` 事件） |
| POST | `/api/tickets/{id}/cancel` | 归属 | 取消（未完成态；未决审批一并 rejected） |
| POST | `/api/tickets/{id}/close` | 归属 | 关闭归档（仅 `done`，UI 已由确认替代） |
| GET | `/api/tickets/{id}/conversation` | 归属 | 全部对话事件（按时间序） |
| GET | `/api/tickets/{id}/events` | 登录 | SSE 事件流 **[deprecated]**（仅兼容旧客户端） |
| WS | `/api/tickets/{id}/ws` | 登录（token 经 query） | WebSocket 实时事件流（**主通道**，先回放再增量） |
| POST | `/api/tickets/{id}/approve` | operator/admin | 审批 `{approval_id, decision: approve|reject, reason}`，返回 `{...,"run":{"status":"queued"}}` |
| GET | `/api/tickets/{id}/approvals` | 归属 | 该工单待审批列表 |

### 9.3 治理 `app/api/governance.py`（prefix `/api`）

| 方法 | 路径 | 权限 | 说明 |
|---|---|---|---|
| GET | `/api/traces/{ticket_id}` | 归属 | Trace 回放（前端 `web/app/traces/[id]`） |
| GET | `/api/metrics` | operator/admin | 指标聚合（start/end/all 筛选） |
| GET | `/api/approvals` | operator/admin | 审批台分页（status: pending/history/all） |
| GET | `/api/routes` | operator/admin | 模型路由查询（intent_type + risk_level） |

### 9.4 知识库 `app/api/kb.py`（prefix `/api/kb`）

| 方法 | 路径 | 权限 | 说明 |
|---|---|---|---|
| GET | `/api/kb` | operator/admin | 文档列表 |
| POST | `/api/kb/upload` | operator/admin | 上传（txt/md/pdf/docx，≤10MB） |
| DELETE | `/api/kb/{doc_id}` | operator/admin | 删除文档 |

### 9.5 MCP `app/api/mcp.py`（prefix `/api/mcp`）

| 方法 | 路径 | 权限 | 说明 |
|---|---|---|---|
| GET | `/api/mcp/servers` | operator/admin | MCP server 状态（不触发连接） |
| GET | `/api/mcp/servers/{name}/tools` | operator/admin | 已发现工具 + 映射后 risk/roles |
| POST | `/api/mcp/servers/{name}/connect` | operator/admin | 手动连接/重连 |
| POST | `/api/mcp/servers/{name}/refresh` | operator/admin | 工具清单/配置热刷新 |
| POST | `/api/mcp/servers/{name}/disconnect` | operator/admin | 断开（故障演练 → 优雅降级） |

### 9.6 健康检查 `app/main.py`

| 方法 | 路径 | 权限 | 说明 |
|---|---|---|---|
| GET | `/health` | 公开 | `{"status":"ok","version":"M3"}` |

**验证方式**：🔌 启动服务后 `curl -s localhost:8000/health`；🔌 全量 API 冒烟见 [测试指南.md](../04-开发指南/测试指南.md) 与 `scripts/rbac_smoke.py`。

---

## 10. 验证命令集

> 🔌 = 离线可测（不依赖真实模型/网络）。命令均在仓库根目录执行。

| 命令 | 验证什么 | 依赖 |
|---|---|---|
| 🔌 `python3 scripts/check_docs.py` | 文档符号级校验（路径/符号/配置键/链接/端口/映射表） | 无 |
| 🔌 `python3 scripts/run_all_tests.py` | 全分支回归：offline 确定性链路（T01/T04/T06/T08-T11/T14/T15）+ 取消流程 | 无 |
| 🔌 `python3 scripts/run_all_tests.py sdk` | SDK 路径：工具循环 + 原生 HITL + 审批恢复 + 驳回收敛 | OpenRouter key（否则降级） |
| 🔌 `python3 scripts/smoke_m2.py` | M2 全链路：低风险收敛（pending_confirm）+ 高风险 HITL + Trace + 指标 | 弱（无 key 走 reader） |
| 🔌 `python3 scripts/rbac_smoke.py` | RBAC：三角色可见范围 / 删除权限（临时 DB 隔离） | 无 |
| 🔌 `python3 scripts/verify_model_config.py` | 模型配置优先级 / 三级熔断 / embedding 优先级 / 键一致性 | 无 |
| `python3 scripts/smoke_agents.py` | SDK 真实链路：多 Agent handoff + 原生 HITL + **跨进程恢复**（清空 `_PENDING` 模拟重启） | OpenRouter key |
| `python3 scripts/smoke_mcp.py` | MCP 端到端真实链路（CmdbAgent handoff） | OpenRouter key + 网络 |

**HTTP 全链路场景（建单 → 跑 → 审批 → 确认 → 回放）**：

```bash
# 0) 启动后端并播种（演示账户密码 demo-pass-2026，可被 SEED_USER_PASSWORD 覆盖）
#    python3 scripts/seed_demo.py && uvicorn app.main:app --port 8000

TOKEN=$(curl -s -X POST localhost:8000/api/auth/login \
  -H 'Content-Type: application/json' \
  -d '{"username":"admin","password":"demo-pass-2026"}' | python3 -c 'import sys,json;print(json.load(sys.stdin)["token"])')

# 1) 建单（高风险：开通只读权限）
TID=$(curl -s -X POST localhost:8000/api/tickets -H "X-Auth-Token: $TOKEN" \
  -H 'Content-Type: application/json' \
  -d '{"title":"给 u-1024 开通数据库只读账号","description":"为 u-1024 申请只读权限","risk_level":"high","intent_type":"change"}' \
  | python3 -c 'import sys,json;print(json.load(sys.stdin)["id"])')

# 2) 触发执行（后台化，立即返回 queued）
curl -s -X POST localhost:8000/api/tickets/$TID/run -H "X-Auth-Token: $TOKEN"
#   → {"status":"queued"}；轮询状态直到 awaiting_approval：
#   curl -s localhost:8000/api/tickets/$TID -H "X-Auth-Token: $TOKEN"

# 3) 审批（operator/admin；approval_id 从 GET /api/tickets/{id}/approvals 取）
AID=$(curl -s localhost:8000/api/tickets/$TID/approvals -H "X-Auth-Token: $TOKEN" \
  | python3 -c 'import sys,json;print(json.load(sys.stdin)[0]["id"])')
curl -s -X POST localhost:8000/api/tickets/$TID/approve -H "X-Auth-Token: $TOKEN" \
  -H 'Content-Type: application/json' \
  -d "{\"approval_id\":$AID,\"decision\":\"approve\"}"
#   → {"decision":"approved","run":{"status":"queued"}}；轮询直到 pending_confirm

# 4) 客户确认完成（pending_confirm → done）
curl -s -X POST localhost:8000/api/tickets/$TID/confirm -H "X-Auth-Token: $TOKEN"
#   → 工单状态 done

# 5) 审计回放：事件流 + Trace
curl -s localhost:8000/api/tickets/$TID/conversation -H "X-Auth-Token: $TOKEN"
curl -s localhost:8000/api/governance/traces/$TID -H "X-Auth-Token: $TOKEN"
```

> 实时进度观察（可选）：WebSocket 主通道 `ws://localhost:8000/api/tickets/$TID/ws?token=$TOKEN`（浏览器 WebSocket 无法自定义 header，token 经 query 传入）；SSE `/api/tickets/{id}/events` 已 deprecated 仅兼容旧客户端。生产部署与端口绑定见 [配置清单.md](../03-部署运维/配置清单.md) 与 [快速开始.md](../00-项目概览/快速开始.md)。

---

## 附：与其他文档的分工

| 主题 | 本文档 | 其他文档 |
|---|---|---|
| 状态机语义/并发安全 | §3 现状事实 | [状态机设计.md](../02-架构设计/状态机设计.md)（为什么这么设计） |
| 多 Agent 分工 | §4.3 清单 | [多Agent编排.md](../02-架构设计/多Agent编排.md)（设计权衡） |
| 治理四件套细节 | §6 现状事实 | [治理层设计.md](../02-架构设计/治理层设计.md) |
| 配置键权威清单 | 引用 | [配置清单.md](../03-部署运维/配置清单.md)（唯一权威源） |
| 能力→代码逐条映射 | §1 锚点 | [能力-代码映射表.md](06-能力-代码映射表.md)（11 条主线全量映射） |
| 测试用例与运行方式 | §10 命令集 | [测试指南.md](../04-开发指南/测试指南.md) |
| 后台任务化/分页 | §4.2 | [04-高并发架构.md](04-高并发架构.md) |
| RAG 检索链路 | 不展开 | [02-RAG知识库.md](02-RAG知识库.md) |
| MCP 工具面 | §6.3 工具清单 | [03-MCP工具集成.md](03-MCP工具集成.md) |
| Trace/指标/日志 | §8 | [05-可观测性与审计.md](05-可观测性与审计.md) |
