"""函数级调用日志埋点（可开关）。

通过环境变量 APP_LOG_LEVEL 控制：
- off（默认）：不输出
- info：记录请求路由 + 核心函数进入/退出
- debug：与 info 相同（保留扩展位，方便后续加参数/返回值）

统一写入项目目录 logs/ 下（可用环境变量 LOG_DIR 改目录，整改⑦），按日期命名文件
（同一天累加到同一文件）：
  logs/2026-08-13.log

时间统一使用北京时间（UTC+8，固定偏移，不依赖系统/容器时区）。
行格式（可 grep 过滤）：
  [10:15:03]  TRACE  enter  orchestrator._run_loop
  [10:15:03]  TRACE  exit   orchestrator._run_loop  ok
  [10:15:03]  REQUEST  POST  /api/tickets  ->  api.tickets.create_ticket
"""
import os
import threading
from datetime import datetime, timedelta, timezone
from functools import wraps
from pathlib import Path

from .config import settings, BASE_DIR

# 北京时间 = UTC+8（固定偏移，避免依赖系统时区与 tzdata）
CN_TZ = timezone(timedelta(hours=8))

_lock = threading.Lock()
_handle = None
_current_date = None


def _now() -> datetime:
    return datetime.now(CN_TZ)


def enabled() -> bool:
    return settings.log_level in ("info", "debug")


def _open_handle() -> None:
    """按北京时间当天日期打开（或复用）日志文件句柄；跨天自动切换新文件。"""
    global _handle, _current_date
    today = _now().strftime("%Y-%m-%d")
    if _handle is not None and _current_date == today:
        return
    if _handle is not None:
        try:
            _handle.close()
        except Exception:
            pass
        _handle = None
    log_dir = Path(settings.log_dir)
    if not log_dir.is_absolute():
        log_dir = BASE_DIR / log_dir
    log_dir.mkdir(parents=True, exist_ok=True)
    path = log_dir / f"{today}.log"
    _handle = open(path, "a", encoding="utf-8")
    _current_date = today


def _emit(line: str) -> None:
    if not enabled():
        return
    global _handle
    try:
        with _lock:
            _open_handle()
            _handle.write(line + "\n")
            _handle.flush()
    except Exception:
        # 日志失败不影响业务；退化为 stderr 以便排查
        try:
            os.write(2, (line + "\n").encode("utf-8", "replace"))
        except Exception:
            pass


def log(kind: str, name: str, *details) -> None:
    if not enabled():
        return
    d = "  ".join(str(x) for x in details if x is not None)
    ts = _now().strftime("%H:%M:%S")
    _emit(f"[{ts}]  TRACE  {kind:<5}  {name}" + (f"  {d}" if d else ""))


def trace_call(name: str):
    """装饰器：函数进入/退出时打点，正常返回与异常均记录，且保留原签名。"""
    def deco(fn):
        @wraps(fn)
        def wrapper(*args, **kwargs):
            log("enter", name)
            try:
                result = fn(*args, **kwargs)
            except Exception as e:  # noqa: BLE001
                log("exit", name, f"error={e.__class__.__name__}: {e}")
                raise
            log("exit", name, "ok")
            return result
        return wrapper
    return deco
