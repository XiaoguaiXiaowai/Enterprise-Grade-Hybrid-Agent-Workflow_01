"""工单意图/风险自动判定（由后端 LLM 判断，替代前端手动选择）。

- 主链路：调用 OpenRouter（OpenAI 兼容 /v1/chat/completions），让 LLM 依据
  标题+内容输出结构化 JSON：{intent_type, risk_level, reason}
- 兜底：无密钥 / 离线 / 解析失败时用关键词规则，保证新建工单仍可用可演示。
"""
from __future__ import annotations
import json
import re

from .config import settings

INTENTS = {"knowledge", "data_query", "change", "troubleshoot"}
RISKS = {"low", "medium", "high"}

# 规则兜底关键词：命中即归类（先意图后风险）
_INTENT_RULES = [
    ("change", ("开通", "授权", "权限", "grant", "只读", "变更", "修改配置", "重置密码", "新建账号", "审批")),
    ("troubleshoot", ("故障", "排查", "连不上", "上不去", "无法访问", "报错", "502", "宕机", "卡顿", "不能联网", "不能上网", "ping")),
    ("data_query", ("数据", "查询", "导出", "报表", "统计", "dataset", "sql", "分析")),
    ("knowledge", ("怎么", "如何", "流程", "文档", "指引", "请教", "咨询", "说明")),
]
_RISK_RULES = [
    ("high", ("上不去网", "宕机", "删除数据", "高危", "敏感", "生产故障", "全线", "崩溃")),
    ("medium", ("变更", "授权", "权限", "开通", "修改配置", "重启", "部署")),
]


def _rule_classify(text: str) -> tuple[str, str]:
    t = text or ""
    intent = "knowledge"
    for name, kws in _INTENT_RULES:
        if any(k in t for k in kws):
            intent = name
            break
    risk = "low"
    for name, kws in _RISK_RULES:
        if any(k in t for k in kws):
            risk = name
            break
    if intent == "change":
        risk = "medium" if risk == "low" else risk
    return intent, risk


def _parse_json(text: str) -> dict | None:
    if not text:
        return None
    m = re.search(r"\{.*\}", text, re.S)
    if not m:
        return None
    try:
        return json.loads(m.group(0))
    except Exception:
        return None


def classify(title: str, description: str) -> dict:
    """返回 {intent_type, risk_level, source, reason}。source: llm | rule"""
    text = f"{title or ''} {description or ''}"
    prompt = (
        "你是企业 IT 工单的意图分类与风险分级器。根据工单的标题和内容，判断：\n"
        "1. intent_type（意图类型），取值只能是：knowledge（知识咨询）、data_query（数据查询）、"
        "change（变更/授权/开通权限）、troubleshoot（故障排查）之一。\n"
        "2. risk_level（风险等级），取值只能是：low、medium、high 之一。凡涉及开通权限、"
        "授权、变更、生产故障、数据处理等一律不能是 low。\n"
        "3. reason：一句话说明判定理由。\n"
        "只输出一个 JSON 对象，不要输出其他内容，格式："
        '{"intent_type":"...","risk_level":"...","reason":"..."}\n\n'
        f"工单标题：{title}\n工单内容：{description}"
    )

    try:
        if settings.openrouter_api_key:
            from openai import OpenAI
            client = OpenAI(
                api_key=settings.openrouter_api_key,
                base_url=settings.openrouter_base_url,
            )
            model = settings.model_name
            resp = client.chat.completions.create(
                model=model, messages=[{"role": "user", "content": prompt}],
                temperature=0, max_tokens=120,
            )
            data = _parse_json(resp.choices[0].message.content or "")
            if data:
                intent = data.get("intent_type")
                risk = data.get("risk_level")
                if intent in INTENTS and risk in RISKS:
                    return {
                        "intent_type": intent,
                        "risk_level": risk,
                        "source": "llm",
                        "reason": data.get("reason", ""),
                    }
    except Exception:
        pass

    intent, risk = _rule_classify(text)
    return {"intent_type": intent, "risk_level": risk,
            "source": "rule",
            "reason": "离线规则判定（LLM 不可用）"}
