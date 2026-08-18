# MCP 工具集成（配置即白名单 · 治理桥接）

> 状态：现行 · 最后校验：2026-08-18 · 校验方式：符号级 + 行为实测（`scripts/smoke_mcp_core.py` 26 断言）
> 一句话：**工具可以长在外部系统里（MCP），但能不能被调、由谁调、调完留什么痕，必须由 Harness 说了算**——MCP 解决「连接」，我们解决「治理」。
> 设计决策与演进见 [MCP接入设计.md](../02-架构设计/MCP接入设计.md)；面试金句与演示话术见 [面试文档.md](../05-面试素材/面试文档.md)。

---

## 1. 架构定位

```
MCP Server（外部进程/服务：CMDB、GitHub…）
        │  JSON-RPC 2.0（stdio / streamable_http）
        ▼
app/mcp/config.py    ← 配置即白名单：mcp_servers.json 声明「工具名 → risk/roles/schema」
app/mcp/servers.py   ← 连接生命周期：懒连接、专用事件循环线程、超时/重试/健康检查
app/mcp/bridge.py    ← 治理桥接：MCP 工具包装成 SDK FunctionTool，走与本地工具同构的治理链
        ▼
   Agent（CmdbAgent / KnowledgeAgent / ChangeAgent）
```

| 模块 | 职责 | 关键函数 |
|---|---|---|
| `app/mcp/config.py` | 读取并校验 `mcp_servers.json`；**未声明工具默认拒绝**；参数 schema 校验 | `load_config` / `_validate_mapping` / `_validate_param_schema` |
| `app/mcp/servers.py` | 懒连接（首次访问才拉起子进程）、专用存活事件循环线程、超时/重试/健康检查、应用关闭清理；失败只降级该 server 不拖垮主流程 | `McpRegistry` / `McpConnection` / `_ThreadLoop` |
| `app/mcp/bridge.py` | 用 SDK `FunctionTool` 手工构造（`params_json_schema` 取 MCP 原始 JSON Schema）；`on_invoke` 走与 `agents_tools._run` 同构的执行体；输出敏感字段脱敏 | `build_mcp_tools` / `_make_function_tool` / `_invoke_mcp` / `_sanitize` |
| `app/api/mcp.py` | 管理 API：状态/工具清单/连接/刷新/断开（故障演练） | `servers` / `connect` / `disconnect` / `refresh` |

## 2. 配置即白名单（mcp_servers.json）

内置两个 server（全量映射见 `mcp_servers.json`）：

| server | transport | 工具（risk） |
|---|---|---|
| `cmdb` | stdio（`.venv/bin/python scripts/mcp_servers/cmdb_server.py`，`CMDB_DATA=${CMDB_DATA}`） | `query_asset`（low）、`list_network_devices`（low）、`update_asset_owner`（high） |
| `github` | streamable_http（`https://api.githubcopilot.com/mcp/`，Bearer `GITHUB_MCP_TOKEN`） | `search_repositories`/`list_repositories`/`get_issue`/`get_issue_comments`（low）、`create_issue`（high） |

配置要点：

```jsonc
{
  "servers": [{
    "name": "cmdb",
    "transport": "stdio",
    "command": ".venv/bin/python",
    "args": ["scripts/mcp_servers/cmdb_server.py"],
    "env": {"CMDB_DATA": "${CMDB_DATA}"},        // ${ENV} 运行时注入，不落库明文
    "client_timeout_seconds": 5,
    "call_timeout_seconds": 15,
    "tools": {
      "query_asset": {
        "risk": "low",                           // high → 自动进 HITL 审批
        "roles": ["employee", "operator", "admin"],
        "description": "...",
        "param_schema": {                        // JSON Schema，additionalProperties:false 严格校验
          "type": "object", "required": ["hostname"],
          "properties": {"hostname": {"type": "string"}}
        }
      }
    }
  }]
}
```

- **总开关**：`MCP_ENABLED`（false 时工具不装配、管理端点返回空，行为回到 M4）；
- **未声明 = 拒绝**：server 暴露的工具若不在 `tools` 映射表内，默认拒绝装配（防工具全量放行）；
- **工具名约定**：装配后以 `server__tool` 双下划线命名（`app/mcp/bridge.py:split_tool_name`）。

## 3. 治理桥接（与本地工具完全一致的治理链）

MCP 工具的 `on_invoke` 执行体与 `agents_tools._run` 同构（`app/mcp/bridge.py:_invoke_mcp`）：

```
预算(consume_step) → 幂等去重(guards) → MCP 调用(带超时) → tool_calls 落库 /
trace(context_source="mcp") / ticket_events → 脱敏(_sanitize) → 返回
```

- **HITL 零改动复用**：映射 `risk=high` → `needs_approval=True` → 直接复用 SDK 原生中断 + 审批/恢复链路；
- **降级只放行非 high**：`ensure_proxies_registered` 在 MCP 不可用时只把低/中风险工具降级为「拒绝并说明」，高风险工具保持不可用（宁可失败不越权）。

## 4. Agent 装配

- `CmdbAgent`（M5 新增）：CMDB 外部工具面（`cmdb__*` 三工具），TriageAgent 新增 `transfer_to_cmdb_agent` handoff（`app/agents_defs.py`）；
- GitHub 只读查询（`github__search_repositories` 等）挂 KnowledgeAgent，写操作 `github__create_issue` 挂 ChangeAgent（high → HITL）；
- 现有本地 Agent 逻辑不动，MCP 工具只做加法。

## 5. 管理 API 与验证

| API | 说明 |
|---|---|
| `GET /api/mcp/servers` | server 状态 + 工具清单（operator/admin） |
| `GET /api/mcp/servers/{name}/tools` | 映射后 risk/roles/描述 |
| `POST /api/mcp/servers/{name}/connect` / `disconnect` | 故障演练：手动拉起/断开 |
| `POST /api/mcp/servers/{name}/refresh` | 工具清单热刷新 |

```bash
python scripts/smoke_mcp_core.py     # 🔌 确定性冒烟（26 断言：连接/白名单/治理/审计/脱敏/预算/管理 API）
python scripts/smoke_mcp_http.py     # 🔌 P2 streamable_http 链路（Bearer 401/工具调用/审计/重连）
python scripts/smoke_mcp.py          # ✅ 真实链路端到端（需 OpenRouter key）
python scripts/diag_mcp_stdio.py     # 🔌 stdio 拉起失败真因诊断
```

## 6. 相关配置键

`MCP_ENABLED` / `MCP_CONFIG_PATH` / `MCP_TIMEOUT_SECONDS` / `GITHUB_MCP_TOKEN` → 见 [配置清单.md](../03-部署运维/配置清单.md) 第 10 节。
