"""模型路由：根据工单意图/风险选择模型与后端（写入 Trace 版本）。

M2 规则：高风险/变更类工单用强模型；低风险知识查询用轻量模型。
可通过环境变量覆盖映射。规则可扩展（如按成本/延迟策略）。
"""
from .config import settings

# 意图 -> (provider, model)；None 表示用默认
ROUTING = {
    "knowledge":     ("ollama", "qwen2.5:7b"),      # 轻量
    "data_query":    ("openai", "gpt-4o-mini"),
    "change":        ("openai", "gpt-4o"),          # 高风险/复杂 → 强模型
    "troubleshoot":  ("openai", "gpt-4o-mini"),
}


def route(intent_type: str, risk_level: str = "low") -> dict:
    provider, model = ROUTING.get(intent_type, ("ollama", settings.model_name))
    # 高风险一律升格强模型（openai gpt-4o），除非未配置则回退默认
    if risk_level == "high" and not settings.openai_api_key:
        provider, model = "ollama", settings.model_name
    return {"provider": provider, "model": model, "intent": intent_type, "risk": risk_level}
