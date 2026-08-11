"""LLM 网关：统一 /v1/chat/completions 协议，支持 OpenAI SDK 与 Ollama 双后端。

- provider="openai"：优先用 OpenAI SDK（openai 包），base_url 可指向 OpenAI 或兼容网关
- provider="ollama"：用 httpx 直连本地 Ollama 的 /v1/chat/completions
- provider="reader"（兜底）：离线返回占位 plan，便于无密钥/离线时演示与测试
"""
from dataclasses import dataclass
from datetime import datetime

from .config import settings


@dataclass
class LLMResult:
    content: str
    model: str
    provider: str
    usage: dict
    latency_ms: int = 0


class _OpenAIBackend:
    def chat(self, messages, model=None, **_kw) -> LLMResult:
        from openai import OpenAI
        client = OpenAI(api_key=settings.openai_api_key or "sk-none",
                        base_url=settings.openai_base_url or None)
        t0 = datetime.now()
        resp = client.chat.completions.create(
            model=model or settings.model_name, messages=messages, temperature=0)
        ms = int((datetime.now() - t0).total_seconds() * 1000)
        usage = resp.usage.model_dump() if resp.usage else {}
        return LLMResult(content=resp.choices[0].message.content or "",
                         model=resp.model, provider="openai",
                         usage=usage, latency_ms=ms)


class _OllamaBackend:
    """httpx 直连 Ollama /v1/chat/completions（Ollama 2.3+ 兼容 OpenAI 协议）。"""
    def chat(self, messages, model=None, **_kw) -> LLMResult:
        import httpx as _httpx
        import json as _json
        url = settings.ollama_base_url.rstrip("/") + "/chat/completions"
        payload = {"model": model or settings.model_name, "messages": messages, "temperature": 0}
        t0 = datetime.now()
        # 注意：沙箱无法外连，调用方失败时应由外层捕获转 reader
        with _httpx.Client(timeout=120) as client:
            r = client.post(url, json=payload)
            r.raise_for_status()
            data = r.json()
        ms = int((datetime.now() - t0).total_seconds() * 1000)
        usage = data.get("usage", {})
        return LLMResult(content=data["choices"][0]["message"]["content"],
                         model=data.get("model", ""), provider="ollama",
                         usage=usage, latency_ms=ms)


class _ReaderBackend:
    """离线占位：根据最后一条 user 消息返回稳定 plan，供无后端时演示与测试。
    若涉及开通/授权领域，注入高风险工具 grant_db_readonly 以演示 HITL。"""
    def chat(self, messages, **_kw) -> LLMResult:
        last_user = next(
            (m["content"] for m in reversed(messages) if m.get("role") == "user"), "")
        # 仅依据 <goal> 标题行做意图判断，避免被 <constraints> 等上下文污染
        _goal = ""
        for _line in last_user.splitlines():
            if _line.strip().startswith("<goal>"):
                _goal = _line.strip()
                break
        if any(k in _goal for k in ("开通", "授权", "权限", "grant", "只读")):
            plan = (
                "PLAN\n"
                "1. 检索知识库：search_kb\n"
                "2. 查询目标用户：query_user_dir\n"
                "3. 发起高风险授权：grant_db_readonly\n"
                "4. 待审批通过后完成，done:true"
            )
        else:
            plan = (
                "PLAN\n"
                "1. 检索企业知识库：search_kb\n"
                "2. 若工单涉及用户/权限，查询用户目录：query_user_dir\n"
                "3. 汇总答案，done:true"
            )
        return LLMResult(content=plan, model="reader/offline", provider="reader",
                         usage={"total_tokens": 0})


class LLMGateway:
    def __init__(self, provider: str | None = None):
        self.provider = provider or settings.model_provider
        self.backends = {"openai": _OpenAIBackend(), "ollama": _OllamaBackend(),
                         "reader": _ReaderBackend()}

    def chat(self, messages: list, model=None, **kw) -> LLMResult:
        backend = self.backends.get(self.provider, self.backends["reader"])
        if self.provider in ("openai", "ollama"):
            try:
                return backend.chat(messages, model=model, **kw)
            except Exception:
                # 真实后端不可用（无密钥/离线/连不上）→ 回退离线，保证 LOOP 可演示
                return self.backends["reader"].chat(messages, **kw)
        return backend.chat(messages, **kw)

    @property
    def name(self) -> str:
        return f"{self.provider}:{settings.model_name}"


def get_llm(provider: str | None = None) -> LLMGateway:
    return LLMGateway(provider)
