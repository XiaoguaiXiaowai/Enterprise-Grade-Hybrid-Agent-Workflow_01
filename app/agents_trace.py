"""OpenAI Agents SDK tracing → 本地 SQLite（与既有 app/trace.py schema 一致）。

你确认的落地形式：SDK 默认把 trace 发给 OpenAI；本项目跑 OpenRouter 没有平台可看，
所以用 set_trace_processors 接管，把每次 Runner.run 的顶层 Trace 转存进本地 traces 表，
前端 web/app/traces/[id] 无需改动即可回放。

兼容 SDK 0.20（TracingProcessor 为 6 方法 ABC）：
- on_trace_start/end 记录/落库顶层 Trace
- on_span_start/end 聚合 span（span.export() 含 span_data/时间/错误）
SDK 不可用时全部为空操作，不影响离线降级路径。
"""
from __future__ import annotations

import json

from . import trace as trace_mod

try:
    from agents.tracing import TracingProcessor, set_trace_processors, set_tracing_disabled
    _SDK_OK = True
except Exception:  # pragma: no cover
    TracingProcessor = object  # type: ignore
    set_trace_processors = None  # type: ignore
    set_tracing_disabled = None  # type: ignore
    _SDK_OK = False


def _span_type(span_export: dict) -> str:
    sd = span_export.get("span_data") or {}
    return sd.get("type", "") or span_export.get("object", "")


def _duration_ms(span) -> int:
    try:
        from agents.tracing import util
        if span.started_at and span.ended_at:
            return max(0, int((util.parse_iso(span.ended_at) - util.parse_iso(span.started_at)).total_seconds() * 1000))
    except Exception:
        pass
    return 0


def _summary(spans: list[dict]) -> list[dict]:
    """把 span export 压成前端展示友好的摘要。"""
    out = []
    for s in spans:
        sd = s.get("span_data") or {}
        out.append({
            "type": sd.get("type", ""),
            "name": sd.get("name", ""),
            "model": sd.get("model", ""),
            "input": str(sd.get("input", ""))[:300],
            "output": str(sd.get("output", ""))[:300],
            "error": s.get("error"),
        })
    return out


class LocalTraceProcessor(TracingProcessor):
    """把 SDK 顶层 Trace 转存进本地 traces 表；span 聚合为摘要（工具级 trace 已由 tools 写）。"""

    def __init__(self):
        self._traces: dict[str, dict] = {}
        self._spans: dict[str, list[dict]] = {}

    def on_trace_start(self, trace) -> None:
        self._traces[trace.trace_id] = {
            "name": getattr(trace, "name", ""),
            "metadata": dict(getattr(trace, "metadata", {}) or {}),
            "group_id": getattr(trace, "group_id", None),
        }
        self._spans[trace.trace_id] = []

    def on_trace_end(self, trace) -> None:
        try:
            self._write(trace)
        except Exception:
            pass  # 落库失败不应影响工单主流程
        finally:
            self._traces.pop(trace.trace_id, None)
            self._spans.pop(trace.trace_id, None)

    def on_span_start(self, span) -> None:
        pass

    def on_span_end(self, span) -> None:
        try:
            exp = span.export()
            if exp:
                self._spans.setdefault(span.trace_id, []).append(exp)
        except Exception:
            pass

    def shutdown(self) -> None:
        self._traces.clear()
        self._spans.clear()

    def force_flush(self) -> None:
        pass

    def _write(self, trace) -> None:
        from .agents_runner import PROMPT_VERSION  # 运行期导入，避免模块加载期环（整改⑤）
        meta = self._traces.get(trace.trace_id, {}).get("metadata", {})
        ticket_id = meta.get("ticket_id")
        if ticket_id is None:
            return
        spans = self._spans.get(trace.trace_id, [])
        gen = next((s for s in spans if _span_type(s) in ("generation", "response")), {})
        gsd = gen.get("span_data") or {}
        usage = gsd.get("usage") or {}
        model = gsd.get("model") or meta.get("model", "")
        req_id = meta.get("req_id") or trace.trace_id[:12]
        trace_mod.write(
            req_id=req_id,
            ticket_id=int(ticket_id),
            model=model,
            provider=meta.get("provider", "openrouter"),
            prompt_version=meta.get("prompt_version", PROMPT_VERSION),
            io={"final_output": meta.get("final_output", ""),
                "workflow": meta.get("workflow", ""),
                "spans": _summary(spans)},
            state_before=meta.get("state_before", ""),
            state_after=meta.get("state_after", ""),
            latency_ms=sum(_duration_ms_export(s) for s in spans),
            token_usage=usage,
            context_source="agents_sdk",
        )


def _duration_ms_export(span: dict) -> int:
    try:
        from datetime import datetime
        a = datetime.fromisoformat(span["started_at"]) if span.get("started_at") else None
        b = datetime.fromisoformat(span["ended_at"]) if span.get("ended_at") else None
        if a and b:
            return max(0, int((b - a).total_seconds() * 1000))
    except Exception:
        pass
    return 0


def install() -> None:
    """安装本地 trace processor（替换默认外传）。SDK 不可用时为空操作。"""
    if not _SDK_OK:
        return
    try:
        set_tracing_disabled(False)
        set_trace_processors([LocalTraceProcessor()])
    except Exception:
        pass
