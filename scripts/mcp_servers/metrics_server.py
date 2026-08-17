"""示例 MCP Server（streamable_http 传输）：集群指标查询（只读）——P2 远程链路演示。

对比 cmdb_server.py（stdio，由 client 拉起子进程）：
- 本 server 是**独立的 HTTP 服务**，streamable_http 传输，client 通过 URL 连接（R5 外挂优先）。
- 演示 **Bearer 鉴权**：`METRICS_MCP_TOKEN` 环境变量注入密钥；mcp 的 TransSecSettings
  只防 DNS rebinding/CSR，业务鉴权由本文件用一层 ASGI 中间件承担。

启动（示例端口/token 可被 auth 配置覆盖）：
    METRICS_PORT=8766 METRICS_MCP_TOKEN=demo-secret \
        .venv/bin/python scripts/mcp_servers/metrics_server.py
"""
import os

from mcp.server.mcpserver import MCPServer

# ---------------- 数据 ----------------
_NODES = [
    {"name": "web-node-01", "role": "web", "cpu": 0.42, "mem": 0.61, "alive": True},
    {"name": "web-node-02", "role": "web", "cpu": 0.35, "mem": 0.58, "alive": True},
    {"name": "db-node-01", "role": "db", "cpu": 0.77, "mem": 0.82, "alive": True},
    {"name": "db-node-02", "role": "db", "cpu": 0.12, "mem": 0.45, "alive": False},
]

server = MCPServer("metrics")


@server.tool()
def list_nodes() -> list[dict]:
    """列出集群节点及健康状态（只读）。"""
    return _NODES


@server.tool()
def get_node_load(node: str) -> dict:
    """查询指定节点的 CPU/内存负载（只读）。"""
    for n in _NODES:
        if n["name"] == node:
            return {"node": node, "cpu": n["cpu"], "mem": n["mem"], "alive": n["alive"]}
    return {"node": node, "error": "unknown node"}


# ---------------- HTTP + Bearer 鉴权 ----------------
def _with_bearer(app, token: str):
    """包一层 ASGI 鉴权：Authorization: Bearer <token>，否则 401。"""
    import json as _json

    async def wrapped(scope, receive, send):
        if scope["type"] == "http":
            headers = dict((k.lower(), v) for k, v in scope["headers"])
            auth = headers.get(b"authorization", b"")
            if auth != b"Bearer " + token.encode():
                body = _json.dumps({"detail": "unauthorized"}).encode()
                await send({"type": "http.response.start",
                            "status": 401,
                            "headers": [(b"content-type", b"application/json"),
                                        (b"content-length", str(len(body)).encode())]})
                await send({"type": "http.response.body", "body": body})
                return
        await app(scope, receive, send)

    return wrapped


def build_app(host: str = "127.0.0.1"):
    """返回带 Bearer 鉴权的 ASGI app（供冒烟脚本 import 复用）。"""
    raw = server.streamable_http_app(host=host)
    return _with_bearer(raw, os.environ.get("METRICS_MCP_TOKEN", ""))


if __name__ == "__main__":
    import uvicorn

    port = int(os.environ.get("METRICS_PORT", "8766"))
    uvicorn.run(build_app(), host="127.0.0.1", port=port, log_level="warning")
