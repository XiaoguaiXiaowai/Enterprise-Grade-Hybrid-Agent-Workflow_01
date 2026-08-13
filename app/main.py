"""FastAPI 应用入口：初始化 DB、注册路由、启动播种演示账户。"""
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request

from .db import init_db
from .kb import init_kb, seed_kb
from . import tracelog
from .api import auth as auth_api
from .api import tickets as tickets_api
from .api import governance as governance_api
from .api import kb as kb_api
from .skills import kb, user_dir, grant  # noqa: F401  确保工具注册到注册表

# 内置知识库种子（演示用；生产可改为外部数据源）
DEFAULT_KB = [
    {"title": "VPN 连接不上排查", "tag": "network",
     "content": "1.确认已安装企业根证书 2.检查是否使用公司内网账号 3.联系网络组开通网络策略"},
    {"title": "数据库只读权限开通流程", "tag": "db",
     "content": "申请需填写用途，DBA 审批后发放最小权限账号；只读账号严禁写操作；默认 30 天回收。"},
    {"title": "生产服务 502 排查", "tag": "ops",
     "content": "先查网关与上游健康检查，确认最近一次发布窗口，若有回滚点优先回滚再定位。"},
]


@asynccontextmanager
async def lifespan(app: FastAPI):
    init_db()
    init_kb()
    # 幂等种入知识库（仅当为空时）：先 FTS，再尽力做向量索引
    with __import__("app.db", fromlist=["session"]).session() as conn:
        cnt = conn.execute("SELECT COUNT(*) c FROM kb_fts").fetchone()["c"]
    if cnt == 0:
        seed_kb(DEFAULT_KB)
    try:
        from . import vector_kb
        if vector_kb.vector_enabled() is False and vector_kb._VECTOR_OK:
            vector_kb.index_docs(DEFAULT_KB)
    except Exception:
        pass
    auth_api.seed_users()
    yield


app = FastAPI(title="Enterprise Agent Harness Demo", version="0.1.0-M3", lifespan=lifespan)
app.include_router(auth_api.router)
app.include_router(tickets_api.router)
app.include_router(governance_api.router)
app.include_router(kb_api.router)


@app.middleware("http")
async def trace_requests(request: Request, call_next):
    """请求级埋点：记录 method + path（精确端点函数名由各端点 @trace_call 输出）。"""
    tracelog.log("REQUEST", request.method, request.url.path)
    return await call_next(request)


@app.get("/health")
def health():
    return {"status": "ok", "version": "M3"}
