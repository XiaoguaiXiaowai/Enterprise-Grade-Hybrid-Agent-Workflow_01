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

    if settings.openrouter_api_key:
        try:
            result = chat_with_fallback(
                [{"role": "user", "content": prompt}],
                primary_model=settings.relevance_model,
                fallback_model=settings.relevance_fallback_model,
                fallback_base_url=settings.ollama_base_url,
                temperature=0, max_tokens=120,
                raise_if_reader=True,
            )
            data = _parse_json(result.content or "")
            if enabled():
                log("info", "classify", f"prompt={prompt}")
                log("info", "classify", f"resp={result.content}")
                log("info", "classify", f"data={data}")
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

def relevance_check(title: str, description: str) -> dict:
    """建单前相关性确认：输入与『企业 IT 工单』领域是否相关。

    返回 {relevant: bool, reason: str, source: str}。LLM 明确判定"不相关"才拒绝
    （relevant=False）；LLM 不可用/模糊判定时一律放行（relevant=True, source=rule），
    避免误拦正常工单。
    """
    text = f"{title or ''} {description or ''}"
    prompt = (
        "你是企业 IT 工单系统的入口校验器。判断用户提交的内容是否属于『企业 IT 工单』"
        "处理范围（如：IT 故障排查、知识库/运维咨询、账号与权限开通回收、数据查询、"
        "网络/系统/数据库相关变更等）。\n"
        "明显不相关的情况（如闲聊、招聘、销售、无关提问）才判为 not_relevant。\n"
        "只输出一个 JSON 对象，不要输出其他内容，格式：\n"
        '{"relevant": true, "reason": "一句话理由"}\n'
        "其中 relevant 取值 true（相关）或 false（不相关）。\n\n"
        f"标题：{title}\n内容：{description}"
    )

    if settings.openrouter_api_key:
        try:
            log("info", "relevance_check", f"prompt={prompt}")
            log("info", "relevance_check", f"primary_model={settings.relevance_model}")
            log("info", "relevance_check", f"fallback_model={settings.relevance_fallback_model}")
            result = chat_with_fallback(
                [{"role": "user", "content": prompt}],
                primary_model=settings.relevance_model,
                fallback_model=settings.relevance_fallback_model,
                fallback_base_url=settings.ollama_base_url,
                temperature=0, max_tokens=120,
                raise_if_reader=True,
            )
            data = _parse_json(result.content or "")
            if enabled():
                log("info", "relevance_check", f"result={result}")
                log("info", "relevance_check", f"data={data}")
            if data and isinstance(data.get("relevant"), bool):
                log("info", "relevance_check", "LLM")
                return {"relevant": data["relevant"], "reason": data.get("reason", ""),
                        "source": "llm"}
        except Exception:
            pass
            log("info", "relevance_check", "LLM 异常")

    # 规则兜底：命中已知 IT 关键词即视为相关；否则放行（宁放过不误拦）
    relevant = any(k in text for k in _INTENT_RULES[0][1]) or \
        any(k in text for k in _RISK_RULES[0][1])
    # 明显的非 IT 闲聊/无关信号 → 判定不相关（即便 LLM 无返回也能拦截典型闲聊）
    if not relevant and any(k in text for k in
                             ("情书", "午饭", "吃什么", "招聘", "买菜", "唱歌", "做饭",
                              "恋爱", "电影", "旅游", "股票", "聊聊天", "随便聊聊")):
        log("info", "relevance_check", "规则1")
        return {"relevant": False, "reason": "该内容与『企业 IT 工单』无关",
                "source": "rule"}
    log("info", "relevance_check", "规则2")
    return {"relevant": True, "reason": "离线规则：无法确认相关性时放行",
            "source": "rule"}

def rewrite_content(title: str, description: str) -> dict:
    """提示词重写：把原始工单内容总结归纳成简洁的目标，再并入提示词。

    返回 {summary, keywords, source}。LLM 不可用时原样返回原始文本，保证流程不中断。
    """
    text = f"{title or ''} {description or ''}"
    prompt = (
        "你是企业 IT 工单系统的内容整理器。把用户提交的原始工单内容改写为简洁、结构化的"
        "任务目标，供下游 Agent 使用。要求：\n"
        "1. 保持原始诉求的关键信息（对象、动作、目标），不增删事实；\n"
        "2. 用 1-2 句话概括核心任务；\n"
        "3. 提取 3~6 个关键词（含可能涉及的用户/账号/主机等实体）。\n"
        "只输出一个 JSON 对象，不要输出其他内容，格式：\n"
        '{"summary": "简洁任务目标", "keywords": ["kw1", "kw2"]}\n\n'
        f"标题：{title}\n内容：{description}"
    )

    if settings.openrouter_api_key:
        try:
            log("info", "rewrite_content", f"prompt={prompt}")
            log("info", "rewrite_content", f"primary_model={settings.rewrite_model}")
            log("info", "rewrite_content", f"fallback_model={settings.rewrite_fallback_model}")
            result = chat_with_fallback(
                [{"role": "user", "content": prompt}],
                primary_model=settings.rewrite_model,
                fallback_model=settings.rewrite_fallback_model,
                fallback_base_url=settings.ollama_base_url,
                temperature=0, max_tokens=180,
                raise_if_reader=True,
            )
            data = _parse_json(result.content or "")
            if enabled():
                log("info", "rewrite_content", f"result={result}")
                log("info", "rewrite_content", f"data={data}")
            if data and data.get("summary"):
                log("info", "rewrite_content", "LLM")
                return {"summary": data["summary"],
                        "keywords": data.get("keywords", []), "source": "llm"}
        except Exception:
            log("info", "rewrite_content", "LLM 异常")
            pass
    log("info", "rewrite_content", "规则")
    return {"summary": text, "keywords": [], "source": "rule"}
