"""FastAPI 应用入口：初始化 DB、注册路由、启动播种演示账户。"""
from contextlib import asynccontextmanager

from fastapi import FastAPI

from .db import init_db
from .api import auth as auth_api
from .api import tickets as tickets_api
from .api import governance as governance_api
from .skills import kb, user_dir, grant  # noqa: F401  确保工具注册到注册表


@asynccontextmanager
async def lifespan(app: FastAPI):
    init_db()
    auth_api.seed_users()
    yield


app = FastAPI(title="Enterprise Agent Harness Demo", version="0.1.0-M2", lifespan=lifespan)
app.include_router(auth_api.router)
app.include_router(tickets_api.router)
app.include_router(governance_api.router)


@app.get("/health")
def health():
    return {"status": "ok", "version": "M2"}
