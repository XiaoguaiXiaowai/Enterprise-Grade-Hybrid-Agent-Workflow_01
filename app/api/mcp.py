"""MCP 管理路由：外部工具面的状态 / 工具清单 / 连接控制（M5）。

演示讲点：
- `GET /api/mcp/servers`：外部工具面一屏可见（server 状态 / 已发现工具 / 映射表）
- `GET /api/mcp/servers/{name}/tools`：动态工具也过白名单（映射后的 risk/roles）
- `POST /api/mcp/servers/{name}/connect|disconnect`：故障演练（断开 cmdb → 优雅降级）
"""
from fastapi import APIRouter, Depends, HTTPException

from ..deps import require_role
from ..mcp import servers as mcp_servers
from ..tracelog import trace_call, log_exception

router = APIRouter(prefix="/api/mcp", tags=["mcp"])


def _remote_hint(name: str) -> str:
    """远程 server 连接失败的提示（区分「需独立启动」与 stdio 自动拉起）。"""
    raw = mcp_servers.registry.list_server_configs()
    entry = next((r for r in raw if r["name"] == name), None)
    if entry and entry.get("transport") in ("streamable_http", "sse"):
        return ("（该 server 是远程服务，不会由本应用拉起——需先独立启动，"
                "如：METRICS_PORT=8766 METRICS_MCP_TOKEN=<token> "
                "python scripts/mcp_servers/metrics_server.py，"
                "并确认 .env 的 METRICS_PORT 与之一致）")
    return ""


@router.get("/servers")
@trace_call("api.mcp.servers")
def servers(ctx: dict = Depends(require_role("operator", "admin"))):
    """配置的 MCP server 列表 + 连接状态（不触发连接）。"""
    return mcp_servers.registry.status()


@router.get("/servers/{name}/tools")
@trace_call("api.mcp.servers_tools")
def server_tools(name: str, ctx: dict = Depends(require_role("operator", "admin"))):
    """某 server 的已发现工具清单 + 映射后的 risk/roles（白名单语义）。"""
    st = mcp_servers.registry.status(name=name)
    if not st:
        raise HTTPException(status_code=404, detail=f"未配置 MCP server: {name}")
    entry = st[0]
    mappings = entry.get("tools", {})
    # tools 字段在 status() 里是已发现工具名列表；映射配置取其 risk/roles
    raw = mcp_servers.registry.list_server_configs()
    detail = next((r for r in raw if r["name"] == name), {})
    tool_names = mappings if isinstance(mappings, list) else []
    return {
        "name": name,
        "healthy": entry.get("healthy"),
        "error": entry.get("error"),
        "tools": [
            {"name": t, **(detail.get("tools", {}).get(t, {}) or {})}
            for t in tool_names
        ],
    }


@router.post("/servers/{name}/connect")
@trace_call("api.mcp.servers_connect")
def connect(name: str, ctx: dict = Depends(require_role("operator", "admin"))):
    """手动触发连接（懒连接之外的手动预连 / 断后重连演示）。"""
    try:
        conn = mcp_servers.registry.get_connection(name, refresh=True)
    except mcp_servers.McpUnavailable as e:
        log_exception()
        raise HTTPException(status_code=404, detail=str(e))
    if not conn.healthy:
        raise HTTPException(status_code=502,
                            detail=f"连接失败: {conn.error}{_remote_hint(name)}")
    return {"name": name, "healthy": True, "tools": sorted(conn.tools)}


@router.post("/servers/{name}/refresh")
@trace_call("api.mcp.servers_refresh")
def refresh(name: str, ctx: dict = Depends(require_role("operator", "admin"))):
    """工具清单/配置热刷新（P2）：重建连接并重新 list_tools。

    场景：server 新增/下线了工具，或 mcp_servers.json 改了映射表——无需重启，
    调用本端点即可让该 server 的可用工具集与配置保持最新（缓存 cache_tools_list 重建）。
    """
    try:
        conn = mcp_servers.registry.get_connection(name, refresh=True)
    except mcp_servers.McpUnavailable as e:
        log_exception()
        raise HTTPException(status_code=404, detail=str(e))
    return {"name": name, "healthy": conn.healthy, "error": conn.error,
            "tools": sorted(conn.tools)}


@router.post("/servers/{name}/disconnect")
@trace_call("api.mcp.servers_disconnect")
def disconnect(name: str, ctx: dict = Depends(require_role("operator", "admin"))):
    """断开连接（演示外部系统故障 → 优雅降级，再 connect 恢复）。"""
    mcp_servers.registry.disconnect(name)
    return {"name": name, "healthy": False, "note": "已断开（下次调用会按需重连）"}