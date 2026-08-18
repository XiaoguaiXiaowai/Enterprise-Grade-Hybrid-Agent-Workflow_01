"""MCP 配置层：加载 `mcp_servers.json` 并校验（MCP 工具映射白名单的**唯一事实源**）。

设计（见 doc/MCP集成设计方案.md）：
- MCP server 暴露的工具**不能自动全量放行**；只有在本文件的 `tools` 映射表里显式声明的
  工具才可被装配（未声明 = 拒绝），与 `skills/registry.py` 的硬编码白名单同语义（R2）。
- 每个映射项声明 `risk`（low/medium/high）与 `roles`（允许角色），字段语义与
  `skills/registry.Tool` 完全对齐。
- `headers` / `env` 中的 `${ENV_VAR}` 占位在加载时从进程环境展开——密钥不落 JSON、
  不进 git（R3/R6 同级安全要求）。
"""
from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from ..config import BASE_DIR, settings
from ..tracelog import log_exception

TRANSPORTS = ("stdio", "streamable_http", "sse")
RISKS = ("low", "medium", "high")
ROLES = ("employee", "operator", "admin")

_ENV_RE = re.compile(r"\$\{([A-Za-z_][A-Za-z0-9_]*)\}")


def _expand_env(value: Any) -> Any:
    """递归展开字符串中的 ${ENV_VAR}（缺失时替换为空串）。"""
    if isinstance(value, str):
        return _ENV_RE.sub(lambda m: os.getenv(m.group(1), ""), value)
    if isinstance(value, dict):
        return {k: _expand_env(v) for k, v in value.items()}
    if isinstance(value, list):
        return [_expand_env(v) for v in value]
    return value


@dataclass
class ToolMapping:
    """server 内一个工具的治理映射（与 skills/registry.Tool 对齐）。"""

    name: str
    risk: str = "low"                                   # low | medium | high
    roles: tuple = ("employee", "operator", "admin")    # 允许装配进上下文的角色
    description: str = ""                               # 可覆盖 server 自带描述
    param_schema: dict | None = None                    # 可选：JSON Schema 子集，调用前校验（P1）


@dataclass
class ServerConfig:
    """一个 MCP server 的完整配置（已展开环境变量）。"""

    name: str
    transport: str                      # stdio | streamable_http | sse
    command: str = ""                 # stdio：可执行文件（须为仓库内相对路径或绝对路径）
    args: list[str] = field(default_factory=list)
    cwd: str = "."                    # stdio：子进程工作目录（相对项目根）
    env: dict[str, str] = field(default_factory=dict)
    url: str = ""                     # streamable_http / sse：端点地址
    headers: dict[str, str] = field(default_factory=dict)
    client_timeout_seconds: float = 5.0    # SDK 连接/session 超时
    max_retry_attempts: int = 1            # 连接尝试次数（超过即标记 unhealthy）
    call_timeout_seconds: float | None = None  # 单次工具调用超时（None=取 settings.mcp_timeout_seconds）
    tools: dict[str, ToolMapping] = field(default_factory=dict)

    def tool_names(self) -> list[str]:
        return list(self.tools.keys())

    def redacted(self) -> dict[str, Any]:
        """供管理 API / 日志展示：headers、env 只保留键名（值一律遮挡）。"""
        return {
            "name": self.name,
            "transport": self.transport,
            "command": self.command,
            "cwd": self.cwd,
            "url": self.url,
            "headers": {k: "***" for k in self.headers},
            "env": {k: "***" for k in self.env},
            "client_timeout_seconds": self.client_timeout_seconds,
            "max_retry_attempts": self.max_retry_attempts,
            "tools": {
                name: {
                    "risk": mapping.risk,
                    "roles": list(mapping.roles),
                    "description": mapping.description or None,
                    "param_schema": mapping.param_schema,
                }
                for name, mapping in self.tools.items()
            },
        }


class McpConfigError(ValueError):
    """配置缺失/非法：装配层捕获后按「未启用 MCP」处理，不炸主流程。"""


def _validate_mapping(name: str, raw: Any, server: str) -> ToolMapping:
    if not isinstance(raw, dict):
        raise McpConfigError(f"[{server}] 工具映射 '{name}' 必须是对象")
    risk = str(raw.get("risk", "low"))
    if risk not in RISKS:
        raise McpConfigError(f"[{server}] 工具 '{name}' risk 非法: {risk}（须为 {RISKS}）")
    roles_raw = raw.get("roles") or ("employee", "operator", "admin")
    if isinstance(roles_raw, str):
        roles_raw = [roles_raw]
    roles = tuple(str(r) for r in roles_raw)
    bad = [r for r in roles if r not in ROLES]
    if bad:
        raise McpConfigError(f"[{server}] 工具 '{name}' roles 非法: {bad}（须为 {ROLES}）")
    return ToolMapping(
        name=name,
        risk=risk,
        roles=roles,
        description=str(raw.get("description", "")),
        param_schema=_validate_param_schema(raw.get("param_schema"), server, name),
    )


def _validate_param_schema(schema: Any, server: str, tool: str) -> dict | None:
    """校验工具级 param_schema（JSON Schema 子集：type/required/properties/enum）。

    非法即抛 McpConfigError（配置即白名单，坏 schema 拒绝装载而非静默放行）。
    """
    if schema is None:
        return None
    if not isinstance(schema, dict) or schema.get("type") not in ("object", None):
        raise McpConfigError(f"[{server}] 工具 '{tool}' param_schema.type 必须是 'object'")
    props = schema.get("properties") or {}
    if not isinstance(props, dict):
        raise McpConfigError(f"[{server}] 工具 '{tool}' param_schema.properties 必须是对象")
    required = schema.get("required") or []
    if not isinstance(required, list) or not all(isinstance(r, str) for r in required):
        raise McpConfigError(f"[{server}] 工具 '{tool}' param_schema.required 必须是字符串数组")
    allowed_types = {"string", "number", "integer", "boolean", "array", "object", "null"}
    for pname, psch in props.items():
        if not isinstance(psch, dict):
            raise McpConfigError(f"[{server}] 工具 '{tool}' 属性 '{pname}' 描述非法")
        if "type" in psch and psch["type"] not in allowed_types:
            raise McpConfigError(
                f"[{server}] 工具 '{tool}' 属性 '{pname}' type 非法: {psch['type']}")
    return {"type": "object", "properties": props, "required": required,
            "additionalProperties": schema.get("additionalProperties", False)}


def _validate_server(raw: dict[str, Any]) -> ServerConfig:
    name = str(raw.get("name", "")).strip()
    if not name:
        raise McpConfigError("MCP server 配置缺少 name")
    transport = str(raw.get("transport", "")).strip()
    if transport not in TRANSPORTS:
        raise McpConfigError(f"[{name}] transport 非法: {transport!r}（须为 {TRANSPORTS}）")
    tools_raw = raw.get("tools")
    if not isinstance(tools_raw, dict) or not tools_raw:
        raise McpConfigError(f"[{name}] tools 映射表不能为空（MCP 工具必须显式声明才可装配）")

    cfg = ServerConfig(
        name=name,
        transport=transport,
        tools={},
    )
    if transport == "stdio":
        cfg.command = str(raw.get("command", "")).strip()
        if not cfg.command:
            raise McpConfigError(f"[{name}] stdio 必须配置 command")
        # 防任意命令：允许仓库内相对路径（如 .venv/bin/python）或绝对路径
        cmd_path = Path(cfg.command)
        if not cmd_path.is_absolute() and (".." in Path(cfg.command).parts):
            raise McpConfigError(f"[{name}] stdio command 不允许跨目录: {cfg.command}")
        cfg.args = [str(a) for a in (raw.get("args") or [])]
        cfg.cwd = str(raw.get("cwd") or ".")
        # 传给子进程的 env：剔除展开后为空的键——`${VAR}` 未设置时会展开成空串，
        # 若照传会造成歧义（如 cmdb_server 把空 CMDB_DATA 当 "." 读目录）。
        cfg.env = {str(k): str(v) for k, v in (raw.get("env") or {}).items()
                   if str(v) != ""}
    elif transport in ("streamable_http", "sse"):
        cfg.url = str(raw.get("url", "")).strip()
        if not cfg.url:
            raise McpConfigError(f"[{name}] {transport} 必须配置 url")
        cfg.headers = {str(k): str(v) for k, v in (raw.get("headers") or {}).items()}

    cfg.client_timeout_seconds = float(raw.get("client_timeout_seconds", 5.0))
    cfg.max_retry_attempts = max(0, int(raw.get("max_retry_attempts", 1)))
    ct = raw.get("call_timeout_seconds")
    cfg.call_timeout_seconds = None if ct is None else float(ct)

    for tool_name, mapping_raw in tools_raw.items():
        mapping = _validate_mapping(str(tool_name), mapping_raw, name)
        cfg.tools[mapping.name] = mapping
    return cfg


def load_config(path: str | Path | None = None) -> dict[str, ServerConfig]:
    """加载并校验 mcp_servers.json；返回 {name: ServerConfig}。

    - 无配置/配置非法/未启用 → 返回空 dict（上层按「无 MCP 工具」处理，绝不抛）。
      （McpConfigError 的调用方用 `mcp_enabled` 判断是否要记日志。）
    - ${ENV} 在 `_validate_server` 之前整体展开。
    """
    cfg_path = Path(path or settings.mcp_config_path)
    if not cfg_path.is_absolute():
        cfg_path = BASE_DIR / cfg_path
    if not cfg_path.exists():
        return {}
    try:
        raw = json.loads(cfg_path.read_text(encoding="utf-8"))
    except Exception:
        log_exception()
        return {}
    raw = _expand_env(raw)
    servers_raw = raw.get("servers") if isinstance(raw, dict) else None
    if not isinstance(servers_raw, list):
        return {}
    out: dict[str, ServerConfig] = {}
    for item in servers_raw:
        if not isinstance(item, dict):
            continue
        try:
            cfg = _validate_server(item)
        except McpConfigError:
            log_exception()
            continue  # 单个坏配置不拖垮其它 server（R5 外挂优先原则）
        out[cfg.name] = cfg
    return out