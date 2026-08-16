"""工单意图/风险自动判定（由后端 LLM 判断，替代前端手动选择）。

- 主链路：OpenRouter（OpenAI 兼容 /v1/chat/completions），让 LLM 依据标题+内容输出
  结构化 JSON：{intent_type, risk_level, reason}
- 兜底：主用失败 → Ollama 兜底 → 再失败才用关键词规则，保证新建工单仍可用可演示。
"""
from __future__ import annotations
import json
import re

from .config import settings
from .llm_gateway import chat_with_fallback
from .tracelog import enabled, log
from .prompts.classifier import (
    INTENT_RULES as _INTENT_RULES,
    RISK_RULES as _RISK_RULES,
    NON_IT_KEYWORDS,
    classify_prompt,
    relevance_prompt,
    rewrite_prompt,
    followup_relevance_prompt,
)

INTENTS = {"knowledge", "data_query", "change", "troubleshoot"}
RISKS = {"low", "medium", "high"}


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

def classify(title: str = "", description: str = "", rewritten: str | None = None) -> dict:
    """返回 {intent_type, risk_level, source, reason}。source: llm | rule

    rewritten 提供时（提示词重写后的简洁目标）将作为分类对象，否则用原始标题+内容。
    """
    raw = f"{title or ''} {description or ''}"
    focus = (rewritten or "").strip() or raw
    prompt = classify_prompt(focus)

    if settings.openrouter_api_key:
        try:
            result = chat_with_fallback(
                [{"role": "user", "content": prompt}],
                primary_model=settings.relevance_model,
                fallback_model=settings.relevance_fallback_model,
                fallback_base_url=settings.ollama_base_url,
                temperature=settings.llm_temperature,
                max_tokens=settings.llm_max_tokens_classify,
                raise_if_reader=True,
            )
            data = _parse_json(result.content or "")
            if enabled():
                log("info", "classify", f"resp={result.content}")
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

    intent, risk = _rule_classify(focus)
    return {"intent_type": intent, "risk_level": risk,
            "source": "rule",
            "reason": "离线规则判定（LLM 不可用）"}

def relevance_check(title: str, description: str) -> dict:
    """建单前相关性确认：输入与『企业 IT 工单』领域是否相关。

    返回 {relevant: bool, reason: str, source: str}。LLM 明确判定"不相关"才拒绝
    （relevant=False）；LLM 不可用/模糊判定时一律放行（relevant=True, source=rule），
    避免误拦正常工单。
    """
    text = f"{title or ''} {description or ''}"
    prompt = relevance_prompt(title, description)

    if settings.openrouter_api_key:
        try:
            result = chat_with_fallback(
                [{"role": "user", "content": prompt}],
                primary_model=settings.relevance_model,
                fallback_model=settings.relevance_fallback_model,
                fallback_base_url=settings.ollama_base_url,
                temperature=settings.llm_temperature,
                max_tokens=settings.llm_max_tokens_classify,
                raise_if_reader=True,
            )
            data = _parse_json(result.content or "")
            if enabled():
                log("info", "relevance_check", f"result={result}")
            if data and isinstance(data.get("relevant"), bool):
                log("info", "relevance_check", "LLM 判定")
                return {"relevant": data["relevant"], "reason": data.get("reason", ""),
                        "source": "llm"}
        except Exception:
            log("info", "relevance_check", "LLM 异常")
            pass

    # 规则兜底：命中已知 IT 关键词即视为相关；否则放行（宁放过不误拦）
    relevant = any(k in text for k in _INTENT_RULES[0][1]) or \
        any(k in text for k in _RISK_RULES[0][1])
    # 明显的非 IT 闲聊/无关信号 → 判定不相关（即便 LLM 无返回也能拦截典型闲聊）
    if not relevant and any(k in text for k in NON_IT_KEYWORDS):
        log("info", "relevance_check", "兜底1")
        return {"relevant": False, "reason": "该内容与『企业 IT 工单』无关",
                "source": "rule"}
    log("info", "relevance_check", "兜底2")
    return {"relevant": True, "reason": "离线规则：无法确认相关性时放行",
            "source": "rule"}

def followup_relevance_check(ticket_title: str, ticket_desc: str,
                             recent_dialogue: str, content: str) -> dict:
    """追加提问前相关性确认：追加内容是否与当前工单相关。

    返回 {relevant: bool, reason: str, source: str}。
    - LLM 明确判定不相关才拒绝（relevant=False）；
    - LLM 不可用/模糊判定时一律放行（relevant=True, source=rule），避免误拦正常追问。
    """
    prompt = followup_relevance_prompt(ticket_title, ticket_desc,
                                       recent_dialogue, content)

    if settings.openrouter_api_key:
        try:
            result = chat_with_fallback(
                [{"role": "user", "content": prompt}],
                primary_model=settings.relevance_model,
                fallback_model=settings.relevance_fallback_model,
                fallback_base_url=settings.ollama_base_url,
                temperature=settings.llm_temperature,
                max_tokens=settings.llm_max_tokens_classify,
                raise_if_reader=True,
            )
            data = _parse_json(result.content or "")
            if enabled():
                log("info", "followup_relevance_check", f"result={result.content}")
            if data and isinstance(data.get("relevant"), bool):
                return {"relevant": data["relevant"],
                        "reason": data.get("reason", ""), "source": "llm"}
        except Exception:
            log("info", "followup_relevance_check", "LLM 异常")
            pass

    # 规则兜底：明确的非 IT 闲聊信号 → 不相关；其余一律放行（宁放过不误拦）
    if any(k in content for k in NON_IT_KEYWORDS):
        return {"relevant": False,
                "reason": "该内容与当前工单无关（疑似闲聊）", "source": "rule"}
    return {"relevant": True, "reason": "离线规则：无法确认相关性时放行",
            "source": "rule"}


def rewrite_content(title: str, description: str) -> dict:
    """提示词重写：把原始工单内容总结归纳成简洁的目标，再并入提示词。

    返回 {summary, keywords, source}。LLM 不可用时原样返回原始文本，保证流程不中断。
    """
    text = f"{title or ''} {description or ''}"
    prompt = rewrite_prompt(title, description)

    if settings.openrouter_api_key:
        try:
            result = chat_with_fallback(
                [{"role": "user", "content": prompt}],
                primary_model=settings.rewrite_model,
                fallback_model=settings.rewrite_fallback_model,
                fallback_base_url=settings.ollama_base_url,
                temperature=settings.llm_temperature,
                max_tokens=settings.llm_max_tokens_rewrite,
                raise_if_reader=True,
            )
            data = _parse_json(result.content or "")
            if enabled():
                log("info", "rewrite_content", f"result={result}")
            if data and data.get("summary"):
                log("info", "rewrite_content", "LLM 判定")
                return {"summary": data["summary"],
                        "keywords": data.get("keywords", []), "source": "llm"}
        except Exception:
            log("info", "rewrite_content", "LLM 异常")
            pass
    log("info", "rewrite_content", "兜底")
    return {"summary": text, "keywords": [], "source": "rule"}
