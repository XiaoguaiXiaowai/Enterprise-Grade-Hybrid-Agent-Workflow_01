"""LLM 网关：统一 /v1/chat/completions 协议，支持 OpenAI SDK 与 Ollama 双后端。

- 提供公共熔断工具 build_openai_client / chat_with_fallback：
  「主用(OpenRouter) → Ollama 兜底 → 离线 reader」三级降级，供 classifier 与传统网关复用
- provider="openai"：openai 包，base_url 指向 OpenRouter
- provider="ollama"：httpx 直连 Ollama 的 /v1/chat/completions
- provider="reader"（兜底）：离线返回占位 plan，便于无密钥/离线时演示与测试
- 架构改善阶段1-5：全局并发信号量（AGENT_LLM_CONCURRENCY）+ 单请求超时
  （AGENT_LLM_TIMEOUT），防止高并发下同时打爆 LLM 上游、雪崩到降级链。
"""
import threading
from dataclasses import dataclass
from datetime import datetime

from .config import settings
from .tracelog import log_exception

# 全局 LLM 并发闸门：超出并发数的新请求在信号量处排队等待
_llm_semaphore = threading.BoundedSemaphore(
    max(1, getattr(settings, "agent_llm_concurrency", 4)))


@dataclass
class LLMResult:
    content: str
    model: str
    provider: str
    usage: dict
    latency_ms: int = 0


def build_openai_client(base_url: str | None = None, api_key: str | None = None):
    """构造 OpenAI 兼容客户端；优先 OpenRouter 配置，可按需覆盖 base_url/api_key。"""
    from openai import OpenAI
    return OpenAI(
        api_key=api_key or settings.openrouter_api_key or "sk-none",
        base_url=base_url or settings.openrouter_base_url,
    )


def _llm_timeout() -> float:
    """单请求超时（秒）：AGENT_LLM_TIMEOUT 配置，默认 60。"""
    return float(getattr(settings, "agent_llm_timeout", 60))


def _ollama_chat_raw(messages: list, model: str, base_url: str,
                     temperature: float = settings.llm_temperature) -> dict:
    """httpx 直连 Ollama /v1/chat/completions（Ollama 2.3+ 兼容 OpenAI 协议）。"""
    import httpx
    url = (base_url or settings.ollama_base_url).rstrip("/") + "/chat/completions"
    payload = {"model": model, "messages": messages, "temperature": temperature}
    with _llm_semaphore:
        with httpx.Client(timeout=_llm_timeout()) as client:
            r = client.post(url, json=payload)
            r.raise_for_status()
            return r.json()


def chat_with_fallback(messages: list, primary_model: str, fallback_model: str,
                       base_url: str | None = None, fallback_base_url: str | None = None,
                       temperature: float = settings.llm_temperature,
                       max_tokens: int | None = None,
                       api_key: str | None = None,
                       reader_builder=None, raise_if_reader=False) -> LLMResult:
    """三级降级：主用 OpenAI 兼容 → Ollama 兜底 → 离线 reader。

    返回 LLMResult；reader 兜底也失败（或 raise_if_reader=True）时抛最后一次异常之外的
    RuntimeError，便于调用方显式走规则等末级降级。
    """
    error = None
    # 1) 主用（OpenRouter / OpenAI 兼容）
    if primary_model:
        try:
            res = _openai_chat(messages, primary_model, base_url=base_url,
                               api_key=api_key, temperature=temperature,
                               max_tokens=max_tokens)
            if res.content and res.content.strip():
                return res
            # 空输出（如推理模型被 max_tokens 截断、finish_reason=length）视为失败，
            # 继续走 Ollama 兜底，避免下游误判为成功而静默落规则。
            raise RuntimeError(
                f"主用 {primary_model} 返回空内容（可能被 max_tokens 截断或推理未完成）")
        except Exception as e:  # noqa: BLE001
            log_exception()
            error = e
    # 2) Ollama 兜底
    if fallback_model:
        try:
            data = _ollama_chat_raw(messages, fallback_model,
                                    base_url=fallback_base_url or settings.ollama_base_url,
                                    temperature=temperature)
            return _llm_result_from_ollama_data(data, messages)
        except Exception as e:  # noqa: BLE001
            log_exception()
            error = e
    # 3) 离线 reader
    if reader_builder is not None:
        return reader_builder(messages)
    if raise_if_reader:
        raise RuntimeError(f"主用与 Ollama 兜底均失败: {error}")
    return _ReaderBackend().chat(messages)


def _openai_chat(messages: list, model: str, base_url: str | None = None,
                 api_key: str | None = None, temperature: float = settings.llm_temperature,
                 max_tokens: int | None = None) -> LLMResult:
    client = build_openai_client(base_url=base_url, api_key=api_key)
    t0 = datetime.now()
    kwargs = {"model": model, "messages": messages, "temperature": temperature}
    if max_tokens is not None:
        kwargs["max_tokens"] = max_tokens
    with _llm_semaphore:
        resp = client.chat.completions.create(
            **kwargs, timeout=_llm_timeout())
    ms = int((datetime.now() - t0).total_seconds() * 1000)
    usage = resp.usage.model_dump() if resp.usage else {}
    return LLMResult(content=resp.choices[0].message.content or "",
                     model=resp.model, provider="openrouter", usage=usage, latency_ms=ms)


def _llm_result_from_ollama_data(data: dict, messages: list) -> LLMResult:
    usage = data.get("usage", {})
    return LLMResult(content=data["choices"][0]["message"]["content"],
                     model=data.get("model", ""), provider="ollama", usage=usage)


class _OpenAIBackend:
    def chat(self, messages, model=None, **_kw) -> LLMResult:
        return _openai_chat(messages, model or settings.agent_model)


class _OllamaBackend:
    """httpx 直连 Ollama /v1/chat/completions（Ollama 2.3+ 兼容 OpenAI 协议）。"""
    def chat(self, messages, model=None, **_kw) -> LLMResult:
        data = _ollama_chat_raw(messages, model or settings.agent_fallback_model,
                                base_url=settings.ollama_base_url)
        return _llm_result_from_ollama_data(data, messages)


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
        self.provider = provider or ("openrouter" if settings.openrouter_api_key else "reader")
        self.backends = {"openai": _OpenAIBackend(), "ollama": _OllamaBackend(),
                         "reader": _ReaderBackend()}

    def chat(self, messages: list, model=None, **kw) -> LLMResult:
        # 统一三级降级：主用(OpenRouter) → Ollama 兜底 → reader
        return chat_with_fallback(
            messages,
            primary_model=model or settings.agent_model,
            fallback_model=settings.agent_fallback_model,
            fallback_base_url=settings.ollama_base_url,
            reader_builder=lambda msgs: self.backends["reader"].chat(msgs),
        )

    @property
    def name(self) -> str:
        return f"{self.provider}:{settings.agent_model}"


def get_llm(provider: str | None = None) -> LLMGateway:
    return LLMGateway(provider)
