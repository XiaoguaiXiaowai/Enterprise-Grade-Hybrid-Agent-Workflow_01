"""工单路由：CRUD + 触发运行 + 事件/SSE + 对话记录。租户隔离 + RBAC。"""
import asyncio
import json
import threading

from fastapi import APIRouter, Depends, HTTPException, WebSocket, WebSocketDisconnect
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field

from .. import daos
from ..deps import current_user, require_role
from ..security import resolve_token
from .. import daos as _daos
from ..orchestrator import run_ticket, resume_ticket
from ..classifier import classify, relevance_check, rewrite_content, followup_relevance_check
from ..config import settings as _settings
from ..tracelog import trace_call, enabled, log

router = APIRouter(prefix="/api/tickets", tags=["tickets"])

RISK_LEVELS = {"low", "medium", "high"}
INTENTS = {"knowledge", "data_query", "change", "troubleshoot"}

# 追加提问并发保护：同一工单同时只能有一个追加回合在跑
_followup_locks: dict[int, threading.Lock] = {}
_followup_locks_guard = threading.Lock()


def _followup_lock(ticket_id: int) -> threading.Lock:
    with _followup_locks_guard:
        return _followup_locks.setdefault(ticket_id, threading.Lock())


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
    requester_name: str | None = None
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


def _ensure_access(row, ctx: dict) -> None:
    """租户隔离 + 角色可见范围：
    - 租户不符 → 403
    - employee 仅可访问自己创建/归属的工单；operator/admin 可访问租户内全部工单
    """
    if row["tenant_id"] != ctx["tenant_id"]:
        raise HTTPException(status_code=403, detail="无权访问")
    if ctx["role"] == "employee" and row["requester_id"] != ctx["user_id"]:
        raise HTTPException(status_code=403, detail="无权访问")


@router.post("", response_model=TicketOut)
@trace_call("api.tickets.create_ticket")
def create_ticket(body: TicketIn, ctx: dict = Depends(current_user)):
    # 建单入口顺序：相关性校验 → 内容重写 → 意图/风险分类
    # 1) 相关性确认（开关可配，默认开）。不相关 → 拒绝建单并提示。
    if _settings.input_relevance_check:
        rel = relevance_check(body.title, body.description)
        if enabled():
            log("info", "create_ticket", f"relevance_check={rel}")
        if not rel["relevant"]:
            raise HTTPException(status_code=422, detail=rel["reason"])

    # 2) 工单内容重写：先把原始内容总结归纳成简洁目标（供分类与后续执行复用）
    rew = rewrite_content(body.title, body.description)
    if enabled():
        log("info", "create_ticket", f"rewrite_content={rew}")

    # 3) 意图/风险自动分类：基于重写后的简洁内容判定（LLM，离线退化为规则）
    cls = classify(body.title, body.description, rewritten=rew["summary"])
    if enabled():
        log("info", "create_ticket", f"classify={cls}")
    risk = cls["risk_level"] if cls["risk_level"] in RISK_LEVELS else "low"
    intent = cls["intent_type"] if cls["intent_type"] in INTENTS else "knowledge"
    tid = daos.create_ticket(
        tenant_id=ctx["tenant_id"], requester_id=ctx["user_id"],
        title=body.title, description=body.description,
        risk_level=risk, intent_type=intent, priority=body.priority,
    )
    daos.add_event(tid, "created",
                   {"title": body.title, "classified": cls}, ctx["username"])
    # 持久化重写结果，执行阶段直接复用为目标，避免重复重写
    daos.add_event(tid, "rewrite",
                   {"summary": rew["summary"], "keywords": rew["keywords"],
                    "source": rew["source"]}, ctx["username"])
    return _serialize(daos.get_ticket(tid))


@router.get("")
@trace_call("api.tickets.list_tickets")
def list_tickets(ctx: dict = Depends(current_user)):
    # 可见范围：employee 仅自己的工单；operator/admin 显示租户内全部工单
    if ctx["role"] == "employee":
        return daos.list_tickets(ctx["tenant_id"], requester_id=ctx["user_id"])
    return daos.list_tickets(ctx["tenant_id"])


@router.delete("/{ticket_id}")
@trace_call("api.tickets.delete_ticket")
def delete_ticket(ticket_id: int, ctx: dict = Depends(require_role("admin"))):
    """删除工单（仅 admin）：级联清除事件/审批/工具调用/追踪/指标等关联数据。"""
    if not daos.delete_ticket(ticket_id):
        raise HTTPException(status_code=404, detail="工单不存在")
    if enabled():
        log("info", "delete_ticket", f"admin {ctx['username']} deleted ticket {ticket_id}")
    return {"deleted": True, "id": ticket_id}


@router.get("/all")
@trace_call("api.tickets.list_all_tickets")
def list_all_tickets(ctx: dict = Depends(require_role("operator", "admin"))):
    """审批台/治理所需：列出全部工单（仅运维/管理员可见）。"""
    return daos.list_tickets(ctx["tenant_id"])


@router.get("/{ticket_id}")
@trace_call("api.tickets.get_ticket")
def get_ticket(ticket_id: int, ctx: dict = Depends(current_user)):
    row = daos.get_ticket(ticket_id)
    if row is None:
        raise HTTPException(status_code=404, detail="工单不存在")
    _ensure_access(row, ctx)
    return _serialize(row)


@router.post("/{ticket_id}/run")
@trace_call("api.tickets.run")
def run(ticket_id: int, ctx: dict = Depends(current_user)):
    row = daos.get_ticket(ticket_id)
    if row is None:
        raise HTTPException(status_code=404, detail="工单不存在")
    _ensure_access(row, ctx)
    result = run_ticket(ticket_id, actor=ctx["username"])
    return result


class FollowupIn(BaseModel):
    content: str = Field(min_length=1, max_length=2000)


def _recent_dialogue(ticket_id: int, limit: int = 6) -> str:
    """取最近几条对话事件拼成简短文本，供追加内容相关性校验参考。"""
    lines = []
    for e in reversed(daos.list_events(ticket_id)):
        try:
            payload = json.loads(e["payload_json"] or "{}")
        except Exception:
            payload = {}
        if e["event_type"] == "user_message":
            lines.append(f"用户：{payload.get('content', '')}")
        elif e["event_type"] == "deliver":
            lines.append(f"助手：{str(payload.get('summary', ''))[:200]}")
        elif e["event_type"] == "model_call" and payload.get("kind") == "final":
            lines.append(f"助手：{str(payload.get('content', ''))[:200]}")
        if len(lines) >= limit:
            break
    return "\n".join(reversed(lines))


@router.post("/{ticket_id}/messages")
@trace_call("api.tickets.followup")
def followup(ticket_id: int, body: FollowupIn, ctx: dict = Depends(current_user)):
    """多轮对话：工单完成后用户追加提问/补充信息。

    - 状态守卫：已关闭/等待管理者确认/处理中 → 409 拒绝
    - 追加内容相关性校验（LLM）：不相关 → 回复拒绝文案，不跑 Agent
    - 相关 → 写入 user_message 事件并触发新一轮 Agent 执行
    """
    with _followup_lock(ticket_id):
        row = daos.get_ticket(ticket_id)
        if row is None:
            raise HTTPException(status_code=404, detail="工单不存在")
        _ensure_access(row, ctx)

        status = row["status"]
        if status == "archived":
            raise HTTPException(status_code=409, detail="工单已关闭，无法追加提问")
        if status == "done":
            raise HTTPException(status_code=409, detail="工单已完成，无法追加提问")
        if status == "awaiting_approval":
            raise HTTPException(status_code=409, detail="工单正在等待管理者确认，暂不能追加提问")
        if status in ("created", "triaged", "gathering", "agent_running", "running"):
            raise HTTPException(status_code=409, detail="工单正在处理中，请稍候再追加")
        # 其余（pending_confirm / failed）放行

        # 写入用户消息事件（对话记录 + Agent 历史上下文都会带上）
        daos.add_event(ticket_id, "user_message",
                       {"content": body.content}, ctx["username"])

        # 追加内容相关性校验：与当前工单不相关 → 回复拒绝
        if _settings.input_relevance_check:
            rel = followup_relevance_check(
                row["title"], row["description"],
                _recent_dialogue(ticket_id), body.content)
            if enabled():
                log("info", "followup", f"relevance_check={rel}")
            if not rel["relevant"]:
                reason = rel.get("reason") or ""
                msg = (f"您追加的内容与当前工单『{row['title']}』不相关，已拒绝处理。"
                       f"如需其他诉求，请新建工单。")
                daos.add_event(ticket_id, "reject_followup",
                               {"reason": reason}, ctx["username"])
                daos.add_event(ticket_id, "system_message",
                               {"content": msg}, "system")
                return {"status": "rejected", "message": msg}

        result = run_ticket(ticket_id, actor=ctx["username"])
        return {"status": "accepted", "run": result}


@router.post("/{ticket_id}/confirm")
@trace_call("api.tickets.confirm")
def confirm(ticket_id: int, ctx: dict = Depends(current_user)):
    """客户确认完成：仅『待确认完成』状态可确认；确认后变更为『已完成』（终态）。"""
    row = daos.get_ticket(ticket_id)
    if row is None:
        raise HTTPException(status_code=404, detail="工单不存在")
    _ensure_access(row, ctx)
    if row["status"] != "pending_confirm":
        raise HTTPException(status_code=409, detail="仅『待确认完成』状态的工单可以确认完成")
    daos.transition(ticket_id, "done", ctx["username"], reason="客户确认完成")
    daos.add_event(ticket_id, "confirmed", {"by": ctx["username"]}, ctx["username"])
    return _serialize(daos.get_ticket(ticket_id))


@router.post("/{ticket_id}/close")
@trace_call("api.tickets.close")
def close(ticket_id: int, ctx: dict = Depends(current_user)):
    """关闭工单：仅『已完成』状态可关闭（归档备用路径，UI 已由确认完成替代）。"""
    row = daos.get_ticket(ticket_id)
    if row is None:
        raise HTTPException(status_code=404, detail="工单不存在")
    _ensure_access(row, ctx)
    if row["status"] != "done":
        raise HTTPException(status_code=409, detail="仅『已完成』状态的工单可以关闭")
    daos.transition(ticket_id, "archived", ctx["username"], reason="用户确认关闭")
    daos.add_event(ticket_id, "closed", {"by": ctx["username"]}, ctx["username"])
    return _serialize(daos.get_ticket(ticket_id))


@router.get("/{ticket_id}/conversation")
#@trace_call("api.tickets.conversation")
def conversation(ticket_id: int, ctx: dict = Depends(current_user)):
    """工单内容模块所需：按时间顺序返回工单全部对话记录。"""
    row = daos.get_ticket(ticket_id)
    if row is None:
        raise HTTPException(status_code=404, detail="工单不存在")
    _ensure_access(row, ctx)
    return [dict(e) for e in daos.list_events(ticket_id)]


@router.get("/{ticket_id}/events")
@trace_call("api.tickets.events")
def events(ticket_id: int, token: str | None = None):
    # 兼容 EventSource（无法带自定义 header）——允许经 query token 复用会话
    ctx = resolve_token(token) if token else None
    if ctx is None:
        raise HTTPException(status_code=401, detail="未登录")
    row = daos.get_ticket(ticket_id)
    if row is None or row["tenant_id"] != ctx["tenant_id"]:
        raise HTTPException(status_code=404, detail="工单不存在")
    if ctx["role"] == "employee" and row["requester_id"] != ctx["user_id"]:
        raise HTTPException(status_code=403, detail="无权访问")
    # 实时流式推送：先返回已存在事件，再长轮询等待新事件附加到 SSE。
    cur = daos.list_events(ticket_id)
    cursor = cur[-1]["id"] if cur else 0

    def gen():
        for e in cur:
            payload = json.loads(e["payload_json"] or "{}")
            yield f"data: {json.dumps({'type': e['event_type'], 'at': e['created_at'], 'payload': payload}, ensure_ascii=False)}\n\n"
        nonlocal_cursor = cursor
        try:
            while True:
                rows = daos.list_events_after(ticket_id, nonlocal_cursor)
                if rows:
                    nonlocal_cursor = rows[-1]["id"]
                    for e in rows:
                        payload = json.loads(e["payload_json"] or "{}")
                        yield f"data: {json.dumps({'type': e['event_type'], 'at': e['created_at'], 'payload': payload}, ensure_ascii=False)}\n\n"
                else:
                    asyncio.sleep(0.3)
        except asyncio.CancelledError:
            return

    return StreamingResponse(gen(), media_type="text/event-stream")


@router.websocket("/{ticket_id}/ws")
@trace_call("api.tickets.ws_events")
async def ws_events(websocket: WebSocket, ticket_id: int, token: str | None = None):
    """WebSocket 实时推送工单事件流：先回放已有事件，再增量推送新的流程事件。

    token 经 query 传入（浏览器 WebSocket 无法自定义 header）。
    """
    ctx = resolve_token(token) if token else None
    if ctx is None:
        await websocket.close(code=4401)
        return
    row = daos.get_ticket(ticket_id)
    if row is None or row["tenant_id"] != ctx["tenant_id"]:
        await websocket.close(code=4404)
        return
    if ctx["role"] == "employee" and row["requester_id"] != ctx["user_id"]:
        await websocket.close(code=4403)
        return
    await websocket.accept()
    cursor = 0
    for e in daos.list_events(ticket_id):
        payload = json.loads(e["payload_json"] or "{}")
        await websocket.send_json({"type": e["event_type"], "at": e["created_at"], "payload": payload})
        cursor = e["id"]
    try:
        while True:
            rows = daos.list_events_after(ticket_id, cursor)
            if rows:
                cursor = rows[-1]["id"]
                for e in rows:
                    payload = json.loads(e["payload_json"] or "{}")
                    await websocket.send_json({"type": e["event_type"], "at": e["created_at"], "payload": payload})
            else:
                await asyncio.sleep(0.3)
    except WebSocketDisconnect:
        return


class ApproveIn(BaseModel):
    approval_id: int
    decision: str = "approve"   # approve | reject
    reason: str = ""


@router.post("/{ticket_id}/approve")
@trace_call("api.tickets.approve")
def approve(body: ApproveIn, ticket_id: int, ctx: dict = Depends(require_role("operator", "admin"))):
    ap = _daos.get_approval(body.approval_id)
    if not ap or ap["ticket_id"] != ticket_id:
        raise HTTPException(status_code=404, detail="审批单不存在")
    if ap["status"] != "pending":
        raise HTTPException(status_code=400, detail="该审批已被处理")
    if body.decision == "reject":
        _daos.decide_approval(body.approval_id, "rejected", ctx["username"], body.reason)
        # SDK 原生审批也需恢复 run 以便 Agent 得知被驳回并调整策略
        result = resume_ticket(ticket_id, body.approval_id,
                               actor=ctx["username"], decision="reject")
        return {"status": "rejected", "run": result}
    _daos.decide_approval(body.approval_id, "approved", ctx["username"], body.reason)
    result = resume_ticket(ticket_id, body.approval_id,
                           actor=ctx["username"], decision="approve")
    return {"decision": "approved", "run": result}


@router.get("/{ticket_id}/approvals", dependencies=[Depends(current_user)])
@trace_call("api.tickets.approvals")
def approvals(ticket_id: int, ctx: dict = Depends(current_user)):
    return [_daos.get_approval(a["id"]) and {**dict(_daos.get_approval(a["id"]))}
            for a in _daos.pending_for_ticket(ticket_id)]
