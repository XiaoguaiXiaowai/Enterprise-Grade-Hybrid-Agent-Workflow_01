"""Agent 后台任务执行器（架构改善阶段 1-1）。

问题：run/追问/审批端点同步执行整个 Agent 循环（40~200 秒），持有 HTTP 线程，
高并发下线程池（默认 40）被占满，登录/列表/轮询等轻请求全部排队。

方案：端点只做校验与入队，立即返回 {status:"queued"}；Agent 循环由本模块的
worker 线程池执行。事件照常写 DB，前端 WS（拉模式 0.3s）/轮询自然拿到增量，
因此前端零改动。

- worker 数：AGENT_WORKERS（默认 4），Agent 并发被池封顶，LLM/DB 压力可控。
- 每工单互斥锁：防止同一工单并发双跑（run 与追问、双 run 等）。
- 异常兜底：执行抛异常时，状态机允许则置 failed 并写 system_message。
"""
import threading
from concurrent.futures import ThreadPoolExecutor

from .config import settings
from .tracelog import log_exception

# 每工单执行锁：进程内有效；多副本部署时依赖数据库乐观锁/状态守卫兜底
_ticket_locks: dict[int, threading.Lock] = {}
_ticket_locks_guard = threading.Lock()


def _lock_for(ticket_id: int) -> threading.Lock:
    with _ticket_locks_guard:
        lock = _ticket_locks.get(ticket_id)
        if lock is None:
            lock = threading.Lock()
            _ticket_locks[ticket_id] = lock
        return lock


def try_acquire(ticket_id: int) -> bool:
    """非阻塞获取该工单执行锁；失败说明已有任务在处理，防止并发双跑。"""
    return _lock_for(ticket_id).acquire(blocking=False)


def release(ticket_id: int) -> None:
    """释放执行锁（由执行体 finally 调用，勿在入队路径提前调用）。"""
    try:
        _lock_for(ticket_id).release()
    except RuntimeError:
        pass


_pool = ThreadPoolExecutor(
    max_workers=max(1, settings.agent_workers),
    thread_name_prefix="agent-run",
)


def submit_ticket_job(ticket_id: int, fn, *args, **kwargs) -> bool:
    """入队执行 Agent 任务；成功返回 True。

    调用方必须先 try_acquire(ticket_id) 成功再入队；执行体结束后自动释放。
    """
    from . import daos

    def _runner():
        try:
            fn(*args, **kwargs)
        except Exception:  # noqa: BLE001
            log_exception()
            try:
                t = daos.get_ticket(ticket_id)
                if t and can_mark_failed(t["status"]):
                    daos.add_event(ticket_id, "system_message",
                                   {"content": "工单处理异常，已标记为失败，可补充信息后重试。"},
                                   "system")
                    daos.transition(ticket_id, "failed", "system",
                                    reason="Agent 后台执行异常")
            except Exception:  # noqa: BLE001
                log_exception()
        finally:
            release(ticket_id)

    _pool.submit(_runner)
    return True


def can_mark_failed(status: str) -> bool:
    """后台异常兜底：仅当状态机允许 failed 转移时置失败（避免破坏终态语义）。"""
    from .state_machine import can_transition
    return can_transition(status, "failed")


def shutdown() -> None:
    _pool.shutdown(wait=False)
