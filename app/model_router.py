"""模型路由：根据工单意图/风险选择模型与后端（写入 Trace 版本）。

M4 规则：
- 用户显式设置 MODEL_NAME 时，所有意图默认用它（OpenRouter 上可切换任意模型）
- 未显式设置时按意图给默认模型：知识/查询/排障用轻量模型，变更类用强模型
- 高风险工单一律升格强模型（priority 超控弱模型）
- Ollama 仅兜底：OpenRouter 不可用时由 agents_runner 自动切换，路由不做静态分流
- 未配置 OpenRouter Key 时退回本地 Ollama，保证离线可跑
"""
from .config import settings

# 默认模型（仅当用户未显式设置 MODEL_NAME 时生效）
DEFAULT_MODEL = "openrouter/free"
HIGH_RISK_MODEL = "openrouter/free"
# 意图 -> 默认模型（低/中风险）
INTENT_MODELS = {
    "knowledge":     DEFAULT_MODEL,
    "data_query":    DEFAULT_MODEL,
    "change":        HIGH_RISK_MODEL,   # 变更类默认就用强模型
    "troubleshoot":  DEFAULT_MODEL,
}


def route(intent_type: str, risk_level: str = "low") -> dict:
    provider, model = "openrouter", settings.model_name
    # 用户未显式设置 MODEL_NAME（仍为默认值 qwen2.5:7b）→ 按意图给默认
    if model == "qwen2.5:7b":
        model = INTENT_MODELS.get(intent_type, DEFAULT_MODEL)
    # 高风险一律升格强模型（除非用户显式配置了强模型，避免降配）
    if risk_level == "high" and model == DEFAULT_MODEL:
        model = HIGH_RISK_MODEL
    # 本地/离线环境（未配置 OPENROUTER_API_KEY）→ 落 Ollama，保持可演示
    if not settings.openrouter_api_key:
        provider, model = "ollama", settings.ollama_model_name
    return {"provider": provider, "model": model, "intent": intent_type, "risk": risk_level}
