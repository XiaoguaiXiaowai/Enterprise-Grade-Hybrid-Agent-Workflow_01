"""治理路由：Trace 回放 + 监控指标 + 模型路由配置。"""
from datetime import date

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
def metrics(start: str | None = None, end: str | None = None, all: bool = False,
            ctx: dict = Depends(require_role("operator", "admin"))):
    """监控指标（支持历史时间筛选）。

    - start/end：YYYY-MM-DD 时间段（含边界）；缺省为当月（1 日 → 今天）。
    - all=1：忽略 start/end，返回全部历史数据。
    返回 {range, summary, daily}：range 为表内最早/最晚日期，
    summary 为时间段汇总，daily 为按天序列（趋势图数据源）。
    """
    if not all and not (start and end):
        today = date.today()
        start = start or today.replace(day=1).isoformat()
        end = end or today.isoformat()
    if all:
        start = end = None
    return {
        "range": metrics_mod.available_range(),
        "summary": metrics_mod.aggregate_range(start, end),
        "daily": metrics_mod.daily_series(start, end),
    }


@router.get("/approvals")
@trace_call("api.governance.approvals")
def approvals(page: int = 1, page_size: int = 10, status: str = "pending",
              ctx: dict = Depends(require_role("operator", "admin"))):
    """审批台一览（分页）：待审批 / 历史审批记录。

    - status: pending（待审批）| history（已通过+已驳回）| 空/all（全部）。
    - 租户隔离（operator/admin 可见本租户全部审批），联表带出工单标题。
    返回 {items, total, page, page_size, pages} 分页包裹（与工单分页契约一致）。
    """
    page = max(page, 1)
    page_size = min(max(page_size, 1), 100)
    if status in ("", "all"):
        statuses = None
    elif status == "history":
        statuses = ["approved", "rejected"]
    elif status == "pending":
        statuses = ["pending"]
    else:
        raise HTTPException(status_code=422, detail="status 仅支持 pending / history / all")

    total = daos.count_approvals(ctx["tenant_id"], statuses=statuses)
    items = daos.list_approvals(ctx["tenant_id"], limit=page_size,
                                offset=(page - 1) * page_size, statuses=statuses)
    return {
        "items": items, "total": total, "page": page,
        "page_size": page_size, "pages": (total + page_size - 1) // page_size,
    }


@router.get("/routes")
@trace_call("api.governance.routes")
def routes(intent_type: str = "knowledge", risk_level: str = "low",
           ctx: dict = Depends(require_role("operator", "admin"))):
    return route(intent_type, risk_level)
