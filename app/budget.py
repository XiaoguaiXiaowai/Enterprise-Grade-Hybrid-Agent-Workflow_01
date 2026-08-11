"""三重预算：步数 / Token / 时间。触顶即终止 LOOP，状态转 failed(budget)。"""
import time
from dataclasses import dataclass

from .config import settings


class BudgetExceeded(Exception):
    def __init__(self, kind: str, limit, used):
        super().__init__(f"budget exceeded: {kind} {used}/{limit}")
        self.kind, self.limit, self.used = kind, limit, used


@dataclass
class Budget:
    ticket_id: int
    max_steps: int
    max_tokens: int
    max_seconds: int
    start: float

    steps: int = 0
    tokens: int = 0

    @classmethod
    def start_for(cls, ticket) -> "Budget":
        return cls(ticket_id=ticket["id"], max_steps=settings.budget_max_steps,
                   max_tokens=settings.budget_max_tokens,
                   max_seconds=settings.budget_max_seconds,
                   start=time.time())

    @property
    def elapsed(self) -> float:
        return time.time() - self.start

    def check(self) -> None:
        if self.steps >= self.max_steps:
            raise BudgetExceeded("steps", self.max_steps, self.steps)
        if self.tokens >= self.max_tokens:
            raise BudgetExceeded("tokens", self.max_tokens, self.tokens)
        if self.elapsed >= self.max_seconds:
            raise BudgetExceeded("time", self.max_seconds, int(self.elapsed))

    def consume_step(self, tokens: int = 0) -> None:
        self.steps += 1
        self.tokens += tokens
        self.check()

    @property
    def left(self) -> dict:
        return {"steps": max(0, self.max_steps - self.steps),
                "tokens": max(0, self.max_tokens - self.tokens),
                "seconds": max(0, int(self.max_seconds - self.elapsed))}
