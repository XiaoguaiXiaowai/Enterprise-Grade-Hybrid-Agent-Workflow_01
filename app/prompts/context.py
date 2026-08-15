"""上下文装配器模板（整改⑧：从 app/context_assembler.py 抽出）。"""
from __future__ import annotations

# 约束文案模板：assemble 时按工单风险级别注入
CONSTRAINTS_TMPL = (
    "工单风险级别:{risk_level}；"
    "仅执行低/中风险操作；高风险动作必须先发起审批，不得直接执行；"
    "工具结果如含敏感信息需脱敏后返回。"
)

# render 输出结构（固定顺序 <goal><state><history><memo><tools><constraints><budget_left>）
def render_lines(ctx: dict) -> list[str]:
    return [
        f"<goal>{ctx['goal']}</goal>",
        f"<state>{ctx['state']}</state>",
        "<history>\n" + "\n".join(f"- {h}" for h in ctx["history"]) + "\n</history>",
        f"<memo>{ctx['memo']}</memo>",
        f"<tools>\n{ctx['tools']}\n</tools>",
        f"<constraints>{ctx['constraints']}</constraints>",
        f"<budget_left>{ctx['budget_left']}</budget_left>",
    ]
