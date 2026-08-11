"""工单路由：CRUD + 触发运行 + 事件/SSE。租户隔离 + RBAC。"""
import json

from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field

from .. import daos
from ..deps import current_user, require_role
from ..security import resolve_token
from .. import daos as _daos
from ..orchestrator import run_ticket, resume_ticket

router = APIRouter(prefix="/api/tickets", tags=["tickets"])

RISK_LEVELS = {"low", "medium", "high"}
INTENTS = {"knowledge", "data_query", "change", "troubleshoot"}


class TicketIn(BaseModel):
    title: str = Field(min_length=2, max_length=200)
    description: str = Field(min_length=2)
    risk_level: str = "low"
    intent_type: str = "knowledge"
    priority: str = "normal"


class TicketOut(BaseModel):
    id: int
    tenant_id: int
    requester_id: int
    title: str
    description: str
    risk_level: str
    intent_type: str
    status: str
    priority: str
    created_at: str
    updated_at: str


def _serialize(row) -> dict:
    d = dict(row)
    return TicketOut(**d).model_dump()


@router.post("", response_model=TicketOut)
def create_ticket(body: TicketIn, ctx: dict = Depends(current_user)):
    risk = body.risk_level if body.risk_level in RISK_LEVELS else "low"
    intent = body.intent_type if body.intent_type in INTENTS else "knowledge"
    tid = daos.create_ticket(
        tenant_id=ctx["tenant_id"], requester_id=ctx["user_id"],
        title=body.title, description=body.description,
        risk_level=risk, intent_type=intent, priority=body.priority,
    )
    daos.add_event(tid, "created", {"title": body.title}, ctx["username"])
    return _serialize(daos.get_ticket(tid))


@router.get("")
def list_tickets(ctx: dict = Depends(current_user)):
    return daos.list_tickets(ctx["tenant_id"])


@router.get("/{ticket_id}")
def get_ticket(ticket_id: int, ctx: dict = Depends(current_user)):
    row = daos.get_ticket(ticket_id)
    if row is None:
        raise HTTPException(status_code=404, detail="工单不存在")
    if row["tenant_id"] != ctx["tenant_id"]:
        raise HTTPException(status_code=403, detail="无权访问")
    return _serialize(row)


@router.post("/{ticket_id}/run")
def run(ticket_id: int, ctx: dict = Depends(current_user)):
    row = daos.get_ticket(ticket_id)
    if row is None:
        raise HTTPException(status_code=404, detail="工单不存在")
    if row["tenant_id"] != ctx["tenant_id"]:
        raise HTTPException(status_code=403, detail="无权访问")
    result = run_ticket(ticket_id, actor=ctx["username"])
    return result


@router.get("/{ticket_id}/events")
def events(ticket_id: int, token: str | None = None):
    # 兼容 EventSource（无法带自定义 header）——允许经 query token 复用会话
    ctx = resolve_token(token) if token else None
    if ctx is None:
        raise HTTPException(status_code=401, detail="未登录")
    row = daos.get_ticket(ticket_id)
    if row is None or row["tenant_id"] != ctx["tenant_id"]:
        raise HTTPException(status_code=404, detail="工单不存在")
    evts = daos.list_events(ticket_id)

    def gen():
        for e in evts:
            payload = json.loads(e["payload_json"] or "{}")
            yield f"data: {json.dumps({'type': e['event_type'], 'at': e['created_at'], 'payload': payload}, ensure_ascii=False)}\n\n"

    return StreamingResponse(gen(), media_type="text/event-stream")


class ApproveIn(BaseModel):
    approval_id: int
    decision: str = "approve"   # approve | reject
    reason: str = ""


@router.post("/{ticket_id}/approve")
def approve(body: ApproveIn, ticket_id: int, ctx: dict = Depends(require_role("operator", "admin"))):
    ap = _daos.get_approval(body.approval_id)
    if not ap or ap["ticket_id"] != ticket_id:
        raise HTTPException(status_code=404, detail="审批单不存在")
    if ap["status"] != "pending":
        raise HTTPException(status_code=400, detail="该审批已被处理")
    if body.decision == "reject":
        _daos.decide_approval(body.approval_id, "rejected", ctx["username"], body.reason)
        return {"status": "rejected"}
    _daos.decide_approval(body.approval_id, "approved", ctx["username"], body.reason)
    result = resume_ticket(ticket_id, body.approval_id, actor=ctx["username"])
    return {"decision": "approved", "run": result}


@router.get("/{ticket_id}/approvals", dependencies=[Depends(current_user)])
def approvals(ticket_id: int, ctx: dict = Depends(current_user)):
    return [_daos.get_approval(a["id"]) and {**dict(_daos.get_approval(a["id"]))}
            for a in _daos.pending_for_ticket(ticket_id)]
