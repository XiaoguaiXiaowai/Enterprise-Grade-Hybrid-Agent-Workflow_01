"""上下文装配器：选 / 压 / 截 + 结构化组织，避免模型"自我污染"。

- 选（select）：只带这一圈真正需要的历史与工具，不全部塞入
- 压（compress）：长 observation/tool 结果先摘要（M2 用截断+摘要占位，可接编排模型）
- 截（truncate）：超过阈值 → Auto-Compact 成一段早期摘要，为关键状态留空间
- 结构化：固定顺序段落 <goal><state><history><memo><artifacts><tools><constraints><budget>
"""
from . import daos
from .config import settings
from .skills import registry
from .prompts.context import CONSTRAINTS_TMPL, render_lines

# 阈值外部化（整改②：CTX_MAX_HISTORY / CTX_MAX_OBS_LEN / CTX_MIN_TRUNCATE）
MAX_HISTORY = settings.ctx_max_history      # 只保留最近 N 条
MAX_OBS_LEN = settings.ctx_max_obs_len      # 单条 observation 超过则压缩
MIN_CTX_TRUNCATE = settings.ctx_min_truncate  # 历史条数超过则触发 auto-compact


def _compress_obs(text: str) -> str:
    if len(text) <= MAX_OBS_LEN:
        return text
    # 压：长内容截到头部+尾部要点（M2 可替换为模型 summarize）
    head = text[:400].replace("\n", " ")
    tail = text[-120].replace("\n", " ")
    return f"[已压缩 {len(text)}→520] {head} … {tail}"


def _auto_compact(history: list[dict]) -> list[dict]:
    """截：早期历史压成一段摘要，保留最近几条关键状态。"""
    reseved = history[-3:]
    early = history[:-3]
    summary_line = {
        "type": "auto_compact",
        "content": f"（早期 {len(early)} 条上下文已压缩为摘要："
                   f"{'; '.join(str(e.get('type',''))+'/'+str(e.get('summary',''))[:40] for e in early[-4:])}）",
    }
    return [summary_line] + reseved


def assemble(ticket, history: list[dict], tools_allowed: list[str],
             budget_left: dict, memo: str = "", goal: str | None = None) -> dict:
    sel = history[-MAX_HISTORY:] if history else []
    if len(history) > MIN_CTX_TRUNCATE:
        sel = _auto_compact(sel)

    tools_spec = []
    for t in registry.list_tools():
        if t.name in tools_allowed:
            tools_spec.append(f"- {t.name}[risk={t.risk}]: {t.description}")

    return {
        "goal": goal if goal is not None else ticket["title"],
        "state": ticket["status"],
        "history": [_compress_obs(e.get("content", str(e))) for e in sel],
        "memo": memo,
        "tools": "\n".join(tools_spec),
        "constraints": _constraints(ticket),
        "budget_left": budget_left,
    }


def _constraints(ticket) -> str:
    return CONSTRAINTS_TMPL.format(risk_level=ticket["risk_level"])


def render(ctx: dict) -> str:
    return "\n".join(render_lines(ctx))
