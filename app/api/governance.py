"""治理路由：Trace 回放 + 监控指标 + 模型路由配置。"""
from fastapi import APIRouter, Depends
from pydantic import BaseModel

from .. import trace as trace_mod, metrics as metrics_mod
from ..deps import current_user
from ..model_router import route

router = APIRouter(prefix="/api", tags=["governance"])


@router.get("/traces/{ticket_id}")
def traces(ticket_id: int, ctx: dict = Depends(current_user)):
    return trace_mod.list_for_ticket(ticket_id)


@router.get("/metrics")
def metrics(day: str | None = None, ctx: dict = Depends(current_user)):
    return metrics_mod.aggregate(day)


@router.get("/routes")
def routes(intent_type: str = "knowledge", risk_level: str = "low",
           ctx: dict = Depends(current_user)):
    return route(intent_type, risk_level)
