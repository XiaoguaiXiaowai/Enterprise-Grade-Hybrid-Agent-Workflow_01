"""LLM 网关：统一 /v1/chat/completions 协议，OpenAI SDK 与 Ollama 双后端。

M1 阶段不启用真实模型调用——Reader 固定返回占位响应，避免依赖密钥/网络，
便于先打通整个 LOOP 骨架。M2 再接入真实后端并启用 model_router。
"""
from dataclasses import dataclass
from .config import settings


@dataclass
class LLMResult:
    content: str
    model: str
    provider: str
    usage: dict


class _ReaderBackend:
    """离线占位：根据最后一条 user 消息，返回一个可解析的 plan。"""
    def chat(self, messages: list, **_kw) -> LLMResult:
        last_user = next(
            (m["content"] for m in reversed(messages) if m.get("role") == "user"), ""
        )
        content = (
            "PLAN\n"
            "1. 检索企业知识库：search_kb\n"
            "2. 若命中则直接作答；否则查询用户目录：query_user_dir\n"
            "3. 汇总答案，done:true"
        )
        return LLMResult(content=content, model="reader/offline",
                         provider="reader", usage={"total_tokens": 0})


class LLMGateway:
    def __init__(self, provider: str | None = None):
        self.provider = provider or settings.model_provider
        self.backend = _ReaderBackend()

    def chat(self, messages: list, **kw) -> LLMResult:
        return self.backend.chat(messages, **kw)

    @property
    def name(self) -> str:
        return f"{self.provider}:{settings.model_name}"


def get_llm() -> LLMGateway:
    return LLMGateway()
