"""确定性状态机：约束工单流转，禁止跳步/非法转移，并记录审计事件。"""
from dataclasses import dataclass, field

VALID_STATUSES = {
    "created", "triaged", "gathering", "agent_running",
    "awaiting_approval", "running", "done", "failed", "cancelled", "archived",
}

# 允许的转移表
TRANSITIONS = {
    "created":          {"triaged"},
    "triaged":          {"gathering", "failed"},
    "gathering":        {"agent_running", "awaiting_approval", "failed", "cancelled"},
    "agent_running":    {"gathering", "awaiting_approval", "done", "failed", "cancelled"},
    "awaiting_approval": {"running", "gathering", "cancelled", "failed"},
    "running":          {"done", "failed", "cancelled"},
    # 多轮对话：完成/失败后用户可追加提问 → 回到 gathering 开新一轮
    "done":             {"archived", "gathering"},
    "failed":           {"archived", "gathering"},
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
    """内存中工单运行态；持久层由 tickets/ticket_events 承载。"""
    ticket_id: int
    status: str = "created"
    events: list = field(default_factory=list)
