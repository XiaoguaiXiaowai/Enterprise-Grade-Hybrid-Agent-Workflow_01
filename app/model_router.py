"""模型路由：根据工单意图/风险选择模型与后端（写入 Trace 版本）。

M4 规则：
- 用户显式设置 AGENT_MODEL 时，所有意图默认用它（OpenRouter 上可切换任意模型）
- 未显式设置时按意图给默认模型：知识/查询/排障用轻量模型，变更类用强模型
- 高风险工单一律升格强模型（priority 超控弱模型）
- Ollama 仅兜底：OpenRouter 不可用时由 agents_runner 自动切换，路由不做静态分流
- 未配置 OpenRouter Key 时退回本地 Ollama，保证离线可跑
"""
from .config import settings

#DEFAULT_OPENROUTER_MODEL = "openrouter/free"
DEFAULT_OPENROUTER_MODEL = ""

# 意图 -> 默认模型（仅当用户未显式设置 AGENT_MODEL 时生效）
INTENT_MODELS = {
    "knowledge":    settings.router_model_default,
    "data_query":   settings.router_model_default,
    "change":       settings.router_model_high_risk,
    "troubleshoot": settings.router_model_default,
}


def route(intent_type: str, risk_level: str = "low") -> dict:
    provider, model = "openrouter", settings.agent_model
    # 用户未显式设置 AGENT_MODEL（仍为默认值 openrouter/free）→ 按意图给默认
    if model == DEFAULT_OPENROUTER_MODEL:
        model = INTENT_MODELS.get(intent_type, settings.router_model_default)
    # 高风险一律升格强模型（除非用户显式配置了强模型，避免降配）
    if risk_level == "high" and model == settings.router_model_default:
        model = settings.router_model_high_risk
    # 本地/离线环境（未配置 OPENROUTER_API_KEY）→ 落 Ollama，保持可演示
    if not settings.openrouter_api_key:
        provider, model = "ollama", settings.agent_fallback_model
    return {"provider": provider, "model": model, "intent": intent_type, "risk": risk_level}
