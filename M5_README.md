# M5 —— MCP 集成（P0 完成）

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
   `POST .../connect|disconnect`（故障演练）。

## 新增 / 变更文件

```
app/mcp/__init__.py            # MCP 包
app/mcp/config.py              # 配置加载/校验 + 工具映射白名单          [新增]
app/mcp/servers.py             # 连接生命周期（懒连接/事件循环/重连）    [新增]
app/mcp/bridge.py              # 治理桥接 + FunctionTool 装配           [新增]
app/api/mcp.py                 # /api/mcp 管理端点                     [新增]
app/agents_defs.py             # + CmdbAgent + handoff                [改]
app/agents_tools.py            # + build_mcp_tools(role, names) 转发   [改]
app/prompts/agents.py          # + SYSTEM_CMDB；TRIAGE/CHANGE 增补     [改]
app/config.py                  # + MCP_ENABLED 等配置                  [改]
app/guards.py                  # 修复 is_duplicate 时间窗比较（格式+时区）[改]
app/main.py                    # 挂载 /api/mcp + 关闭时 disconnect_all [改]
scripts/mcp_servers/cmdb_server.py  # 示例 stdio MCP Server（CMDB）    [新增]
scripts/smoke_mcp_core.py      # 确定性冒烟（21 断言，必过）            [新增]
scripts/smoke_mcp.py           # 真模型端到端冒烟（best-effort）        [新增]
mcp_servers.json               # 示例配置                              [新增]
requirements.txt               # + mcp==2.0.0                         [改]
.env.example                   # + MCP_ENABLED 等                      [改]
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
# ② 真模型端到端（可选）：需可访问 openrouter.ai；真模型有随机性，高风险场景可能需重试
python scripts/smoke_mcp.py
```

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

## 已知边界 / 演进点（P1/P2）

- **降级链路**：传统 LLMGateway 循环暂不接 MCP 工具（`register_mcp_proxies` 列 P1）；
  当前 MCP 工具只在 SDK 主链路可用。
- **参数校验**：映射表 `param_schema`（JSON Schema 子集）字段已预留，P1 接线。
- **远程 server**：`streamable_http` / `sse` 的配置与鉴权（`${ENV}` headers）已支持，
  示例仍是 stdio；P2 补一个 HTTP 演示（如 github MCP server）。
- **前端**：`/api/mcp/servers` 数据已就绪，P2 做「MCP 工具面」页面。
- **工具清单热刷新**：配置变更需重启读取（`load_config()` 每次惰性加载，改 JSON 即生效；
  server 工具清单缓存 `cache_tools_list=True`，需要 `refresh=True` 重连刷新）。