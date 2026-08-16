"""确定性状态机：约束工单流转，禁止跳步/非法转移，并记录审计事件。"""
from dataclasses import dataclass, field

VALID_STATUSES = {
    "created", "triaged", "gathering", "agent_running",
    "awaiting_approval", "running", "pending_confirm",
    "done", "failed", "cancelled", "archived",
}

# 注意：status="running" 为历史遗留状态（M2 早期命名），当前编排统一使用
# "agent_running"，running 仅保留在状态机定义中防止历史数据校验失败，无代码路径转入。

# 允许的转移表
TRANSITIONS = {
    "created":          {"triaged", "cancelled"},
    "triaged":          {"gathering", "failed", "cancelled"},
    "gathering":        {"agent_running", "awaiting_approval", "failed", "cancelled"},
    "agent_running":    {"gathering", "awaiting_approval", "pending_confirm", "failed", "cancelled"},
    "awaiting_approval": {"running", "gathering", "cancelled", "failed"},
    "running":          {"done", "failed", "cancelled"},
    # 待客户确认完成：确认 → done（终态）；继续追问 → gathering 开新一轮；放弃 → cancelled
    "pending_confirm":  {"done", "gathering", "cancelled"},
    # done = 客户确认完成，工单走到最后（不再允许追问）；archived 为归档/关闭备用路径
    "done":             {"archived"},
    # failed 允许补充信息后重试（→ gathering）或放弃（→ cancelled）
    "failed":           {"archived", "gathering", "cancelled"},
    "cancelled":        {"archived"},
    "archived":         set(),
}


class InvalidTransition(Exception):
    def __init__(self, frm: str, to: str):
        super().__init__(f"Cannot transition {frm} -> {to}")
        self.frm, self.to = frm, to


def can_transition(frm: str, to: str) -> bool:
    if frm not in VALID_STATUSES or to not in VALID_STATUSES:
        return False
    return to in TRANSITIONS.get(frm, set())


@dataclass
class TicketState:
    """[deprecated] 内存中工单运行态（保留未使用）。

    实际持久层由 tickets/ticket_events 表承载（daos.transition 单一写入口），
    本类仅为早期设计的残留，无任何引用；后续版本可移除。
    """
    ticket_id: int
    status: str = "created"
    events: list = field(default_factory=list)
