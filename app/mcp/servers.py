"""MCP server 注册表：连接生命周期（懒连接/健康检查/重建）+ 工具清单。

线程模型（关键设计）：
- SDK 的 `MCPServer` 是 async **长连接**：`connect()` 建立 session（stdio 拉起子进程），
  之后的 `list_tools()` / `call_tool()` 复用同一 session。
- Agent run 运行在线程池（无事件循环）；若用 `asyncio.run()` 每次新建 loop，
  首次 connect 建立的 session 会因 loop 关闭而失效。
- 因此本模块维护**一个专用存活事件循环线程** `_LOOP`，所有 server 的
  connect / list_tools / call_tool / cleanup 都 submit 到该 loop，session 跨调用存活。

治理约定：
- 懒连接：首次被访问（装配工具或调用）时才拉起 server，进程启动零依赖（R5 外挂优先）。
- 白名单双保险：构造时给 SDK 挂 `create_static_tool_filter(allowed_tool_names=映射表)`
  （SDK 层兜底），`list_tools` 返回后再按映射表交集过滤一次（本层兜底）。
- 失败降级：连接/调用失败只标记该 server unhealthy + 记录 error，绝不抛到 Agent 主流程。
"""
from __future__ import annotations

import asyncio
import json
import threading
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from ..config import BASE_DIR, settings
from ..tracelog import log, log_exception
from .config import ServerConfig, load_config

try:  # SDK 不可用时模块仍可导入、注册表为空（与 agents_tools._SDK_OK 同风格）
    from agents.mcp import (
        MCPServerSse,
        MCPServerSseParams,
        MCPServerStdio,
        MCPServerStdioParams,
        MCPServerStreamableHttp,
        MCPServerStreamableHttpParams,
        create_static_tool_filter,
    )
    _MCP_OK = True
except Exception:  # pragma: no cover
    log_exception()
    MCPServerStdio = MCPServerStreamableHttp = MCPServerSse = None  # type: ignore
    MCPServerStdioParams = MCPServerStreamableHttpParams = MCPServerSseParams = None  # type: ignore
    create_static_tool_filter = None  # type: ignore
    _MCP_OK = False


class _ThreadLoop:
    """专用存活事件循环：run_forever 于守护线程，供跨调用复用 session。"""

    def __init__(self) -> None:
        self._loop: asyncio.AbstractEventLoop = asyncio.new_event_loop()
        self._thread = threading.Thread(target=self._run, name="mcp-event-loop", daemon=True)
        self._thread.start()

    def _run(self) -> None:
        asyncio.set_event_loop(self._loop)
        self._loop.run_forever()

    def run_coro(self, coro, timeout: float):
        fut = asyncio.run_coroutine_threadsafe(coro, self._loop)
        return fut.result(timeout=timeout)


_LOOP = _ThreadLoop()


class McpUnavailable(RuntimeError):
    """MCP server 不可用（未启用/未配置/连接失败）。"""


@dataclass
class McpConnection:
    """一个 MCP server 的运行态快照（进程内单例）。"""

    name: str
    cfg: Any = None                # ServerConfig
    server: Any = None             # agents.mcp.MCPServer 实例
    tools: dict[str, Any] = field(default_factory=dict)   # 工具名 -> mcp.Tool（已过白名单）
    healthy: bool = False
    error: str = ""
    connected_at: str = ""


class McpRegistry:
    """全局注册表：name -> McpConnection（进程内单例，与 skills/registry 同风格）。"""

    def __init__(self) -> None:
        self._conns: dict[str, McpConnection] = {}
        self._lock = threading.Lock()

    # ---------- 内部：构造 / 连接 ----------

    @staticmethod
    def _build_server(cfg) -> Any:
        """按 transport 构造 MCPServer；统一挂 tool_filter（SDK 层白名单兜底）。"""
        allowed = cfg.tool_names()
        tool_filter = create_static_tool_filter(allowed_tool_names=allowed) if allowed else None
        common = dict(
            name=cfg.name,
            cache_tools_list=True,
            client_session_timeout_seconds=cfg.client_timeout_seconds,
            max_retry_attempts=cfg.max_retry_attempts,
            tool_filter=tool_filter,
        )
        if cfg.transport == "stdio":
            def _to_abs(p: str) -> str:
                pp = Path(p)
                return str(pp) if pp.is_absolute() else str(BASE_DIR / pp)
            return MCPServerStdio(
                MCPServerStdioParams(
                    command=_to_abs(cfg.command), args=[_to_abs(a) for a in cfg.args],
                    cwd=_to_abs(cfg.cwd), env=cfg.env or None,
                ),
                **common,
            )
        if cfg.transport == "streamable_http":
            params = {"url": cfg.url, "headers": cfg.headers or None}
            return MCPServerStreamableHttp(
                MCPServerStreamableHttpParams(**params), **common,
            )
        return MCPServerSse(MCPServerSseParams(url=cfg.url), **common)

    def _try_connect(self, cfg):
        """在专用 loop 中 connect + list_tools；成功返回 (server, tools)，失败抛异常。"""
        server = self._build_server(cfg)
        setup_timeout = (cfg.call_timeout_seconds or settings.mcp_timeout_seconds) \
            + cfg.client_timeout_seconds + 5.0

        async def work():
            await server.connect()
            return await server.list_tools()

        raw_tools = _LOOP.run_coro(work(), timeout=setup_timeout)
        # 第二道白名单：只保留映射表里显式声明的工具
        allowed = set(cfg.tool_names())
        tools = {t.name: t for t in raw_tools if getattr(t, "name", None) in allowed}
        return server, tools

    def _connect(self, cfg, old: McpConnection | None) -> McpConnection:
        conn = McpConnection(name=cfg.name, cfg=cfg)
        last_err = ""
        for attempt in range(max(1, cfg.max_retry_attempts + 1)):
            try:
                server, tools = self._try_connect(cfg)
                conn.server, conn.tools, conn.healthy = server, tools, True
                conn.error = ""
                log("info", "mcp.servers._connect",
                    f"{cfg.name} connected tools={sorted(tools)}")
                return conn
            except Exception as e:  # noqa: BLE001 —— 连接失败绝不向上抛
                log_exception()
                last_err = f"{type(e).__name__}: {e}"
                log("info", "mcp.servers._connect", f"{cfg.name} attempt={attempt} e={last_err}")
        # 全部失败：清理残留 server（防子进程泄漏）
        if conn.server is not None:
            try:
                _LOOP.run_coro(conn.server.cleanup(), timeout=cfg.client_timeout_seconds + 5)
            except Exception:  # noqa: BLE001
                log_exception()
                pass
        if old is not None:
            self._conns[cfg.name] = old  # 保持旧连接对象以保留先前的 healthy 状态
            old.healthy, old.error = False, last_err
            return old
        conn.error = last_err
        return conn

    # ---------- 对外：连接获取 / 调用 / 状态 ----------

    def get_connection(self, name: str, refresh: bool = False) -> McpConnection:
        """懒连接：首次访问才拉起 server；unhealthy 或 refresh=True 时自动重建。

        失败不抛（返回 healthy=False 的连接，error 记录原因），调用方按工具级失败处理。
        """
        if not settings.mcp_enabled or not _MCP_OK:
            raise McpUnavailable("MCP 未启用或 SDK 不可用")
        with self._lock:
            conn = self._conns.get(name)
            if conn is not None and (conn.healthy and not refresh):
                return conn
            cfg = self._get_config(name)
            new_conn = self._connect(cfg, old=conn)
            self._conns[name] = new_conn
            return new_conn

    def _get_config(self, name: str):
        """返回 ServerConfig；未知 server 或未启用时抛 McpUnavailable。"""
        if not settings.mcp_enabled or not _MCP_OK:
            raise McpUnavailable("MCP 未启用或 SDK 不可用")
        from .config import load_config  # 惰性加载：配置变更重启后重新读取
        cfg = load_config().get(name)
        if cfg is None:
            raise McpUnavailable(f"未配置 MCP server: {name}")
        return cfg

    def list_server_configs(self) -> list[dict]:
        """所有已配置 server 的脱敏配置（管理 API 用；不触发连接）。"""
        if not settings.mcp_enabled or not _MCP_OK:
            return []
        try:
            return [cfg.redacted() for cfg in load_config().values()]
        except Exception:  # noqa: BLE001
            log_exception()
            return []

    def status(self, name: str | None = None) -> list[dict]:
        """状态快照（含连接状态与已发现工具）；不触发连接。"""
        if not settings.mcp_enabled or not _MCP_OK:
            return []
        out = []
        for cfg in load_config().values():
            if name is not None and cfg.name != name:
                continue
            conn = self._conns.get(cfg.name)
            if conn is None:
                out.append({"name": cfg.name, "transport": cfg.transport,
                            "healthy": False, "error": "not_connected",
                            "tools": [], **cfg.redacted()})
            else:
                out.append({"name": cfg.name, "transport": cfg.transport,
                            "healthy": conn.healthy, "error": conn.error,
                            "tools": sorted(conn.tools)})
        return out

    # ---------- 工具调用 ----------

    def call_tool(self, conn: McpConnection, tool_name: str, arguments: dict) -> dict:
        """调用 MCP 工具并解析为统一结果（{ok, result|error}）；失败不抛。**

        在专用 loop 上执行 server.call_tool；结果解析：
        - is_error / structured_content（mcp 2.x）优先；
        - 退化到 content 文本块拼接（兼容 v1 风格 text content）。
        """
        assert conn.server is not None
        timeout = conn.cfg.call_timeout_seconds or settings.mcp_timeout_seconds

        async def work():
            return await conn.server.call_tool(tool_name, arguments)

        try:
            res = _LOOP.run_coro(work(), timeout=timeout)
        except asyncio.TimeoutError:
            log_exception()
            return {"ok": False, "error": f"MCP 工具超时（>{timeout}s）: {tool_name}"}
        except Exception as e:  # noqa: BLE001 —— transport 错误等，记为工具失败
            log_exception()
            return {"ok": False, "error": f"{type(e).__name__}: {e}"}
        return _parse_call_tool_result(res, tool_name)

    # ---------- 生命周期 ----------

    def disconnect(self, name: str) -> None:
        """断开并清理（管理端点 / 演示故障演练用）。"""
        with self._lock:
            conn = self._conns.pop(name, None)
        if conn is None or conn.server is None:
            return
        try:
            timeout = (conn.cfg.client_timeout_seconds or 5.0) + 5.0
            _LOOP.run_coro(conn.server.cleanup(), timeout=timeout)
        except Exception as e:  # noqa: BLE001
            log_exception()
            log("info", "mcp.servers.disconnect", f"{name} cleanup e={e}")

    def disconnect_all(self) -> None:
        """应用关闭钩子：清理全部 server（防 stdio 子进程泄漏）。"""
        names = list(self._conns.keys())
        for n in names:
            self.disconnect(n)


def _parse_call_tool_result(res, tool_name: str) -> dict:
    """把 mcp.types.CallToolResult 解析为 {ok, data|error}。

    兼容 mcp 2.x 两种返回形态：
    - structured_content：优先（可能带 `{"result": ...}` 的 result 包装，解包一次）；
    - content 文本块：聚合后若为合法 JSON 则解码为 dict/list（mcp 2.x 对 dict 返回
      通常只走文本块），否则原样字符串。
    """
    is_err = bool(getattr(res, "is_error", False))
    sc = getattr(res, "structured_content", None)
    if sc is not None:
        try:
            data = json.loads(json.dumps(sc, ensure_ascii=False, default=str))
            if isinstance(data, dict) and set(data.keys()) == {"result"}:
                data = data["result"]
            if is_err:
                return {"ok": False, "error": str(data)[:500]}
            return {"ok": True, "data": data}
        except Exception:
            log_exception()
            pass
    text = ""
    for block in (getattr(res, "content", None) or []):
        t = getattr(block, "text", None)
        if isinstance(t, str) and t:
            text = (text + "\n" if text else "") + t
    if is_err:
        return {"ok": False, "error": text or f"MCP 工具返回错误: {tool_name}"}
    if text:
        try:  # 文本块可能是服务端序列化的 JSON
            parsed = json.loads(text)
            if isinstance(parsed, (dict, list)):
                return {"ok": True, "data": parsed}
        except Exception:
            log_exception()
            pass
    return {"ok": True, "data": text}


registry = McpRegistry()