"""治理路由：Trace 回放 + 监控指标 + 模型路由配置。"""
from fastapi import APIRouter, Depends, HTTPException

from .. import trace as trace_mod, metrics as metrics_mod, daos
from ..deps import current_user, require_role
from ..model_router import route
from ..tracelog import trace_call

router = APIRouter(prefix="/api", tags=["governance"])


@router.get("/traces/{ticket_id}")
@trace_call("api.governance.traces")
def traces(ticket_id: int, ctx: dict = Depends(current_user)):
    row = daos.get_ticket(ticket_id)
    if row is None or row["tenant_id"] != ctx["tenant_id"]:
        raise HTTPException(status_code=404, detail="工单不存在")
    # employee 仅可回放自己工单的 Trace
    if ctx["role"] == "employee" and row["requester_id"] != ctx["user_id"]:
        raise HTTPException(status_code=403, detail="无权访问")
    return trace_mod.list_for_ticket(ticket_id)


@router.get("/metrics")
@trace_call("api.governance.metrics")
def metrics(day: str | None = None, ctx: dict = Depends(require_role("operator", "admin"))):
    return metrics_mod.aggregate(day)


@router.get("/routes")
@trace_call("api.governance.routes")
def routes(intent_type: str = "knowledge", risk_level: str = "low",
           ctx: dict = Depends(require_role("operator", "admin"))):
    return route(intent_type, risk_level)
