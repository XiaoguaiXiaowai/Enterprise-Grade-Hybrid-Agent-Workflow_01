"""OpenAI Agents SDK 模型提供方：OpenRouter 主链路 + Ollama 兜底。

实现：自定义 ModelProvider（SDK 稳定接口），按模型名前缀路由：
- `openrouter:...` → OpenAIChatCompletionsModel + OpenRouter AsyncOpenAI 客户端
- `ollama:...`     → OpenAIChatCompletionsModel + Ollama AsyncOpenAI 客户端
- 无前缀 → 默认按 OpenRouter 归一化

不依赖 MultiProvider 等新类，规避版本差异；OpenRouter/Ollama 都是 OpenAI 兼容
`/v1/chat/completions`，用 SDK 的 OpenAIChatCompletionsModel 即可。
SDK 不可用时返回 None，调用方沿用 LLMGateway 兜底（离线可演示）。
"""
from __future__ import annotations

from .config import settings
from .tracelog import log_exception

try:
    from agents import ModelProvider
    from agents.models.openai_chatcompletions import OpenAIChatCompletionsModel
    from openai import AsyncOpenAI
    _SDK_OK = True
except Exception:  # pragma: no cover
    log_exception()
    ModelProvider = None  # type: ignore
    OpenAIChatCompletionsModel = None  # type: ignore
    AsyncOpenAI = None  # type: ignore
    _SDK_OK = False


def sdk_available() -> bool:
    return _SDK_OK


class RouterProvider(ModelProvider if _SDK_OK else object):
    """前缀路由：openrouter: / ollama: → 对应 OpenAI 兼容客户端。"""

    def __init__(self):
        self.openrouter = AsyncOpenAI(
            api_key=settings.openrouter_api_key or "sk-none",
            base_url=settings.openrouter_base_url,
        )
        self.ollama = AsyncOpenAI(
            api_key="ollama",
            base_url=settings.ollama_base_url,
        )

    def get_model(self, model_name: str):
        name = model_name or settings.agent_model
        if name.startswith("ollama:"):
            return OpenAIChatCompletionsModel(
                model=name.removeprefix("ollama:"),
                openai_client=self.ollama,
            )
        # openrouter: 或无前缀 → OpenRouter
        return OpenAIChatCompletionsModel(
            model=name.removeprefix("openrouter:"),
            openai_client=self.openrouter,
        )

    def get_default_model_name(self) -> str:
        return settings.agent_model


def build_provider():
    """构造 SDK ModelProvider；SDK 不可用时返回 None。"""
    return RouterProvider() if _SDK_OK else None


def build_fallback_model():
    """构造 Ollama 兜底模型（SDK 不可用时返回 None）。"""
    if not _SDK_OK:
        return None
    name = settings.agent_fallback_model.removeprefix("ollama:")
    return OpenAIChatCompletionsModel(
        model=name,
        openai_client=AsyncOpenAI(api_key="ollama", base_url=settings.ollama_base_url),
    )
