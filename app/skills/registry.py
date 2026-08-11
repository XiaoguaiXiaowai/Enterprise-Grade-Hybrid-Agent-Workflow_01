"""工具注册表：工具必须在此登记（硬编码白名单），未登记即拒绝——防幻觉调用。"""
from __future__ import annotations
import inspect
from dataclasses import dataclass
from typing import Callable, Any


@dataclass
class Tool:
    name: str
    fn: Callable
    risk: str = "low"                 # low | medium | high
    roles: tuple = ("employee", "operator", "admin")  # 允许装配进上下文的角色
    description: str = ""


_REGISTRY: dict[str, Tool] = {}


def register_tool(name=None, risk="low", roles=None, description=""):
    def deco(fn):
        tool_name = name or fn.__name__
        _REGISTRY[tool_name] = Tool(
            name=tool_name, fn=fn, risk=risk,
            roles=tuple(roles or ("employee", "operator", "admin")),
            description=description or inspect.getdoc(fn) or "",
        )
        return fn
    return deco


def get_tool(name: str) -> Tool | None:
    return _REGISTRY.get(name)


def list_tools() -> list[Tool]:
    return list(_REGISTRY.values())


def tools_for_role(role: str) -> list[str]:
    return [t.name for t in _REGISTRY.values() if role in t.roles]


def invoke(name: str, params: dict, ctx: dict) -> dict:
    """统一入口：未登记即拒绝；调用并包裹结果。"""
    tool = get_tool(name)
    if tool is None:
        return {"ok": False, "error": f"unknown tool: {name}"}
    result = tool.fn(**params)
    return {"ok": True, "tool": name, "result": result}
