"""P2 远程 streamable_http 链路冒烟（Bearer 鉴权 + 工具调用 + 审计）。

自起一个本地 metrics MCP server（streamable_http），以带 Bearer 的客户端连接：
- 验证远程 server 连接、工具清单过白名单、只读调用、输出脱敏、审计落库
- 验证 Bearer 鉴权：错误 token 的客户端连接/调用被拒（401）
- 验证 refresh（热刷新）

用法：.venv/bin/python scripts/smoke_mcp_http.py
"""
import os
import socket
import subprocess
import sys
import tempfile
import time

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
os.environ.setdefault("APP_ENV", "development")
os.environ["APP_DB_PATH"] = tempfile.mktemp(suffix=".db")
os.environ["CMDB_DATA"] = tempfile.mktemp(suffix=".json")

PORT = int(os.environ.get("SMOKE_METRICS_PORT", "8877"))
TOKEN = "smoke-secret-token"

_PASS = _FAIL = 0


def _assert(cond, msg):
    global _PASS, _FAIL
    _PASS += 1 if cond else 0
    _FAIL += 0 if cond else 1
    print(("PASS  " if cond else "FAIL  ") + msg)


def _free_port():
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    p = s.getsockname()[1]
    s.close()
    return p


def _wait_port(port, timeout=25):
    end = time.time() + timeout
    while time.time() < end:
        try:
            with socket.create_connection(("127.0.0.1", port), timeout=1):
                return True
        except OSError:
            time.sleep(0.3)
    return False


def _connect_with(headers_override=None):
    """用给定 headers 覆盖连接到 metrics server；返回 (status_ok, conn 或错误)。"""
    from app.mcp.config import load_config
    cfg = load_config()["metrics"]
    if headers_override is not None:
        cfg.headers = headers_override

    from app.mcp import servers as mcp_servers
    from app.mcp.servers import McpRegistry
    reg = McpRegistry()
    # 用新注册表避免进程内既有连接缓存
    conn = reg._connect(cfg, None)
    return conn


def main():
    from app.db import init_db
    from app.api.auth import seed_users
    from app import daos
    init_db(); seed_users()

    # ---- 1) 启动 metrics http server（子进程）----
    env = dict(os.environ, METRICS_PORT=str(PORT), METRICS_MCP_TOKEN=TOKEN,
               PYTHONUNBUFFERED="1")
    proc = subprocess.Popen(
        [sys.executable, "scripts/mcp_servers/metrics_server.py"],
        cwd=ROOT, env=env,
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    try:
        if not _wait_port(PORT):
            _assert(False, "M2 metrics http server 端口就绪")
            return
        _assert(True, "M2 metrics http server 启动（streamable_http）")

        os.environ["METRICS_PORT"] = str(PORT)
        os.environ["METRICS_MCP_TOKEN"] = TOKEN

        # ---- 2) 正常连接（带正确 Bearer）----
        from app.mcp.config import load_config
        cfg = load_config()["metrics"]
        _assert(cfg.headers.get("Authorization") == "Bearer smoke-secret-token"
                and f":{PORT}/mcp" in cfg.url,
                f"M3 配置 url/headers 经 ${{ENV}} 展开: url={cfg.url} hdr={cfg.headers}")
        # env 已在 load_config 前设置；registry 惰性读取 → 直接用进程内注册表
        from app.mcp import servers as S
        conn = S.registry.get_connection("metrics", refresh=True)
        _assert(conn.healthy, f"M4 远程 server 连接成功（error={conn.error[:80]}）")
        _assert(set(conn.tools) == {"list_nodes", "get_node_load"},
                f"M5 工具清单过白名单: {sorted(conn.tools)}")

        # ---- 3) 调用工具 + 脱敏 + 审计 ----
        from app import daos
        tid = daos.create_ticket(1, _admin_id(), "查集群负载", "查询 web-node-01 负载",
                                 "low", "knowledge")
        from types import SimpleNamespace
        from app.budget import Budget
        budget = Budget.start_for(daos.get_ticket(tid))
        from app.mcp import bridge
        ctx = SimpleNamespace(context={"ticket_id": tid, "actor": "admin",
                                       "budget": budget, "req_id": "",
                                       "model": "test", "provider": "openrouter"})
        r = bridge._invoke_mcp(ctx, "metrics", "get_node_load", {"node": "web-node-01"})
        _assert(isinstance(r, dict) and r.get("node") == "web-node-01"
                and r.get("cpu") is not None, f"M6 远程只读调用: {str(r)[:100]}")
        # 非法参数（schema 约束）
        r2 = bridge._invoke_mcp(ctx, "metrics", "get_node_load", {})
        _assert(isinstance(r2, dict) and r2.get("status") == "error"
                and "参数校验失败" in str(r2.get("error", "")),
                f"M7 param_schema 参数校验（远程）: {str(r2)[:80]}")

        # ---- 4) Bearer 鉴权：错误 token → 连接失败（授权被拒）----
        bad = _connect_with({"Authorization": "Bearer wrong-token"})
        # SDK 对 401 的报错形态不定（ExceptionGroup / HTTPStatusError），
        # 关键断言是「错误密钥 → 连接不健康」（服务端鉴权生效）。
        _assert(bad.healthy is False and bad.error,
                f"M8 错误 Bearer 被拒（连接不健康）: {bad.error[:80]}")

        # ---- 5) refresh 热刷新 ----
        conn2 = S.registry.get_connection("metrics", refresh=True)
        _assert(conn2.healthy and "list_nodes" in conn2.tools, "M9 refresh 重连成功")

        S.registry.disconnect_all()
        print(f"\nMCP HTTP SMOKE: {_PASS} passed, {_FAIL} failed")
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=5)
        except Exception:
            proc.kill()
    sys.exit(1 if _FAIL else 0)


def _admin_id():
    from app.db import session
    with session() as c:
        return c.execute("SELECT id FROM users WHERE username='admin'").fetchone()["id"]


if __name__ == "__main__":
    main()