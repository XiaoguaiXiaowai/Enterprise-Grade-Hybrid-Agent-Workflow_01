# M5 —— MCP 集成（P0 / P1 / P2 完成）

在 M4 基础上，把**外部工具/数据源的标准接入（MCP）**引入工具面：MCP server 的工具
不会直接挂给模型，必须过映射表白名单并享受与本地工具完全一致的治理链路。
设计文档见 [`doc/MCP集成设计方案.md`](doc/MCP集成设计方案.md)。

## 一句话

> 工具可以长在外部系统里（MCP），但能不能被调、由谁调、调完留什么痕，必须由 Harness
> 说了算——**MCP 解决「连接」，我们解决「治理」**。

## 核心实现

1. **配置即白名单**（`app/mcp/config.py` + `mcp_servers.json`）：MCP server 暴露的工具
   不会自动放行；只有映射表里显式声明的（`工具名 → risk / roles`）才可装配。
2. **连接生命周期**（`app/mcp/servers.py`）：懒连接（首次访问才拉起子进程）、专用存活
   事件循环线程（SDK 的 `MCPServer` 是 async 长连接，跨多次调用复用 session）、
   超时/重试/健康检查、应用关闭清理。失败只降级该 server，不拖垮主流程。
3. **治理桥接**（`app/mcp/bridge.py`）：MCP 工具用 SDK `FunctionTool` 手工构造
   （`params_json_schema` 直接取 MCPTool 原始 JSON Schema），`on_invoke` 走与
   `agents_tools._run` 同构的执行体：预算 → 去重 → MCP 调用 → tool_calls 落库 /
   trace(`context_source="mcp"`) / ticket_events → consume_step；输出敏感字段脱敏；
   映射 risk=high → `needs_approval=True` → **复用现有 SDK 原生 HITL 审批/恢复链路，零改动**。
4. **Agent 装配**（`app/agents_defs.py`）：新增 `CmdbAgent`（CMDB 配置库，MCP 外部工具面），
   TriageAgent 新增一个 handoff；现有 4 个 Agent 不动。
5. **管理 API**（`app/api/mcp.py`）：`GET /api/mcp/servers`（状态/工具清单）、
   `GET /api/mcp/servers/{name}/tools`（映射后 risk/roles）、
   `POST .../connect|disconnect`（故障演练）、`POST .../refresh`（工具清单热刷新）。

**P1 —— 治理完善**
6. **参数校验**（`app/mcp/validate.py` + 映射表 `param_schema`）：调用前按 JSON Schema
   子集（type / required / properties / enum / additionalProperties=false）校验，非法参数
   拒绝执行并落审计；SDK 链路与降级链路同套校验器。
7. **降级链路贯通（R4）**（`bridge.ensure_proxies_registered` + `orchestrator` 接线）：
   把只读/低风险 MCP 工具注册进 `skills.registry`，传统 LLMGateway 循环（`_plan_and_execute`）
   也能消费 MCP 工具，治理由 `_execute_tool` 统一承担。**高风险写操作不注册**——强制走
   SDK 原生审批，保持 HITL 语义单一。`_normalize_tool_params` 对 `**kwargs` 代理工具放行透传。

**P2 —— 生态增强**
8. **远程 server + 鉴权**：`streamable_http` 远程链路（`scripts/mcp_servers/metrics_server.py`
   自起演示 + `scripts/smoke_mcp_http.py` 冒烟）带 **Bearer 鉴权**（mcp 2.x 的 TransSec 只防
   DNS/CSR，业务鉴权由一层 ASGI 中间件承担）；客户端 `${ENV}` 注入 Authorization 头。
   **公共 server 示例**：`mcp_servers.json` 内置 GitHub 官方 MCP server
   （`https://api.githubcopilot.com/mcp/`，Bearer 用 `GITHUB_MCP_TOKEN`），
   映射表只放行声明的 5 个工具（search_repositories / list_repositories / get_issue /
   get_issue_comments 只读 + create_issue 写操作→HITL）。
9. **热刷新**：`POST /api/mcp/servers/{name}/refresh` 重建连接并重拉工具清单（含重新读配置）。
10. **前端「MCP 工具面」**（`web/app/mcp/page.tsx`）：一屏展示各 server 状态 / transport /
   已发现工具 / 映射后的 risk·roles·param_schema·描述，支持 连接 / 断开（故障演练）／
   刷新。`web/lib/api.ts` + Nav 已接入。

## 新增 / 变更文件

```
app/mcp/__init__.py            # MCP 包
app/mcp/config.py              # 配置加载/校验 + 工具映射白名单 + param_schema  [新增]
app/mcp/validate.py            # 轻量 JSON Schema 参数校验器（P1）              [新增]
app/mcp/servers.py             # 连接生命周期（懒连接/事件循环/重连/超时重试）  [新增]
app/mcp/bridge.py              # 治理桥接 + FunctionTool + param_schema 校验
                               #   + register_mcp_proxies（降级链路，P1）      [新增]
app/api/mcp.py                 # /api/mcp 管理端点（+ refresh 热刷新，P2）      [新增]
app/agents_defs.py             # + CmdbAgent + handoff                        [改]
app/agents_tools.py            # + build_mcp_tools(role, names) 转发           [改]
app/orchestrator.py            # 降级链路接入 ensure_proxies_registered（P1）  [改]
app/prompts/agents.py          # + SYSTEM_CMDB；TRIAGE/CHANGE 增补             [改]
app/config.py                  # + MCP_ENABLED 等配置                         [改]
app/guards.py                  # 修复 is_duplicate 时间窗比较（格式+时区）      [改]
app/main.py                    # 挂载 /api/mcp + 关闭时 disconnect_all        [改]
scripts/mcp_servers/cmdb_server.py      # 示例 stdio MCP Server（CMDB）       [新增]
scripts/mcp_servers/metrics_server.py   # 示例 streamable_http MCP Server（P2）[新增]
scripts/smoke_mcp_core.py      # 确定性冒烟（22 断言，必过）                    [新增]
scripts/smoke_mcp_http.py      # 远程 http + Bearer 冒烟（8 断言，P2）          [新增]
scripts/smoke_mcp.py           # 真模型端到端冒烟（best-effort）                [新增]
mcp_servers.json               # 示例配置（cmdb stdio + metrics http）          [新增]
web/app/mcp/page.tsx           # 前端「MCP 工具面」页面（P2）                   [新增]
web/lib/api.ts / components/Nav.tsx  # 前端接入 MCP API + 导航                 [改]
requirements.txt               # + mcp==2.0.0                                 [改]
.env.example                   # + MCP_ENABLED / METRICS_* 等                  [改]
```

## 依赖与配置

```bash
pip install -r requirements.txt    # 含 mcp==2.0.0（mcp 2.x，openai-agents 0.20.0 自带 MCP_V2 兼容）
```

`.env` 增量：`MCP_ENABLED=true`、`MCP_CONFIG_PATH=mcp_servers.json`、
`MCP_TIMEOUT_SECONDS=15`；远程 server 鉴权密钥走 `${ENV}` 注入、不落 JSON。

> 注意：mcp 2.x 已移除 FastMCP，示例 server 使用新 `MCPServer` API
> （`@server.tool()` + `run_stdio_async`），与 SDK 客户端 `MCPServerStdio` 天然对接。

## 验证

```bash
. .venv/bin/activate
# ① 确定性冒烟（推荐，必过）：不依赖真模型，覆盖连接/白名单/治理/审计/脱敏/重连
python scripts/smoke_mcp_core.py
# ② 远程 streamable_http + Bearer 鉴权冒烟（P2）：脚本自起 metrics server 验证远程链路
python scripts/smoke_mcp_http.py
# ③ 真模型端到端（可选）：需可访问 openrouter.ai；真模型有随机性，高风险场景可能需重试
python scripts/smoke_mcp.py
```

> ② 会自动在本机拉起 `metrics_server.py`（streamable_http + Bearer）并做鉴权/调用/刷新断言，
> 无需手工启动、也**不依赖 mcp_servers.json 里的任何配置**（脚本自造 ServerConfig）。
> 公共 GitHub server：`mcp_servers.json` 已内置，只需在 `.env` 填 `GITHUB_MCP_TOKEN`（PAT，
> 见 .env.example 注释）重启后端，再到「MCP 工具面」页面点 github 的「连接」。

冒烟断言（core，22 项）：
1. 装配白名单：employee 只装只读 `cmdb__query_asset`；operator 全有；未映射/角色不符拒绝
2. **HITL 接线（确定性）**：高风险 MCP 工具装配即为 `needs_approval=True`（接入 SDK 原生
   审批中断），只读工具为 False
3. 真实 MCP stdio 连接 + 工具清单与映射表一致
4. 治理执行体：只读调用 → 返回资产 / 预算 +1 步 / `tool_calls` 落库 / trace `context_source=mcp`
5. 输出脱敏：owner 等敏感字段打码（`李娜芳` → `李*芳`）
6. 重复动作拦截（`is_duplicate`）与未映射工具调用拒绝
7. `/api/mcp` 状态 / 配置脱敏（headers 不泄漏）
8. 断开 → 下次调用自动重连

> 真模型端到端（smoke_mcp.py）：低风险只读链路稳定通过（CmdbAgent → `cmdb__query_asset`
> → done → trace mcp 记录）；高风险 HITL 场景依赖模型「自愿调用高风险工具」的行为，
> 属 best-effort（多次尝试 + 换主机重试）。HITL 机制本身由以下确定性证据覆盖：
> ① 上述 needs_approval=True（装配层接线）；② `smoke_agents.py` / `run_all_tests.py`
> 中本地高风险工具（grant_db_readonly）走同一 SDK 原生中断+审批恢复链路已验证。

> **顺带修复的既有 bug（M5 治理验证中发现）**：`app/guards.py` 的 `is_duplicate`
> 时间窗口比较此前**永不命中**——SQLite `datetime('now')` 是 UTC 且空格分隔，
> 而原代码用本地时区 isoformat（`T` 分隔）比较 → 本地/格式双重不一致。
> 修后与落库格式/时区对齐，重复动作检测真正生效（本地与 MCP 工具同受益）。

> 已知环境注意：若 `.env` 未配置 `CLASSIFIER_MODEL/REWRITE_MODEL`，OpenRouter 会 400
> 后 fallback 到不可达的远程 Ollama 而卡约 120s——冒烟脚本已注入可用模型并关闭
> 重写/相关性校验（测试目标在 MCP 链路）。

## P1/P2 完成情况与剩余边界

已完成：
- **P1**：参数校验（param_schema）、`register_mcp_proxies`（降级链路贯通 R4）、超时/重试/健康检查、
  `/api/mcp` 端点、文档。
- **P2**：`streamable_http` 远程 server + Bearer 鉴权演示、工具清单热刷新（refresh）、
  前端「MCP 工具面」页面。

仍可选深化（非阻塞）：
- **OAuth**：当前以 Bearer 静态 token 演示鉴权；OAuth 2 授权码流留给真实远程 server（SSE/OAuth
  由 SDK 原生支持，config 已可传 headers/token）。
- **sse 传输**：配置层与 `servers.py` 已支持 `sse` 的 url + headers，示例未单列一个 sse server
  （与 streamable_http 演示等价，视需要再加）。
- **前端工具清单热刷新**已具备（refresh 按钮）；若要更高频自动同步可在页面轮询时透传 refresh。