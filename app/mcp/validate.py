"""MCP 工具参数校验：对 param_schema（JSON Schema 子集）做轻量校验（P1）。

不引入 jsonschema 依赖（保持零新增外部库），只覆盖配置白名单所需的子集：
- type（object/string/number/integer/boolean/array/object/null）
- required（必填数组）
- properties 里的单层枚举 enum 与类型检查
- additionalProperties=false 时拒绝未声明字段

失败返回校验错误列表；成功返回空列表。校验在两个调用面生效：
1. SDK 链路（bridge._invoke_mcp 的 on_invoke 前）
2. 降级链路（register_mcp_proxies 的 proxy fn 前）
"""
from __future__ import annotations

from typing import Any

# 数字/整数：bool 是 int 子类，需显式排除
_TYPE_CHECKERS = {
    "string": lambda v: isinstance(v, str),
    "number": lambda v: isinstance(v, (int, float)) and not isinstance(v, bool),
    "integer": lambda v: isinstance(v, int) and not isinstance(v, bool),
    "boolean": lambda v: isinstance(v, bool),
    "array": lambda v: isinstance(v, list),
    "object": lambda v: isinstance(v, dict),
    "null": lambda v: v is None,
}


def validate_params(schema: dict | None, params: dict) -> list[str]:
    """校验 params 是否符合 schema；返回错误信息列表（空 = 通过）。"""
    if not schema or not isinstance(schema, dict):
        return []
    errors: list[str] = []
    props = schema.get("properties") or {}
    required = schema.get("required") or []
    additional_allowed = bool(schema.get("additionalProperties"))

    # 未声明字段：additionalProperties=false 时拒绝
    if not additional_allowed:
        unknown = [k for k in params if k not in props]
        if unknown:
            errors.append(f"未声明参数: {', '.join(sorted(unknown))}")

    # 必填缺失
    missing = [k for k in required if k not in params]
    if missing:
        errors.append(f"缺少必填参数: {', '.join(missing)}")

    for key, value in params.items():
        psch = props.get(key)
        if not isinstance(psch, dict):
            continue
        # 枚举
        enum = psch.get("enum")
        if isinstance(enum, list) and enum and value not in enum:
            errors.append(f"参数 '{key}' 取值非法: {value!r}（允许 {enum}）")
            continue
        # 类型
        typ = psch.get("type")
        if typ and value is not None:
            checker = _TYPE_CHECKERS.get(typ)
            if checker is not None and not checker(value):
                errors.append(f"参数 '{key}' 类型非法：期望 {typ}")
    return errors