# 新增API指南

> 状态：现行 · 最后校验：2026-08-18 · 校验方式：符号级 + 行为实测
> 定位：新增一个 FastAPI 端点的完整套路——路由注册链路、最小可运行示例、依赖注入与 RBAC、审计落库、测试与验证。
> 前置阅读：[能力-代码映射表.md](../01-核心功能/06-能力-代码映射表.md)（② 业务入口 / ④ 业务出口）；涉及配置键一律引用 [配置清单.md](../03-部署运维/配置清单.md)。
> 技术栈：FastAPI + Pydantic v2（`model_dump`）+ SQLite（daos 层）+ TestClient 冒烟。

## 1. 路由注册链路（api 文件 → main.py include）

链路分两步：

1. 每个领域一个模块放 `app/api/` 下，文件内定义 `router = APIRouter(prefix=..., tags=...)`；
2. `app/main.py` 中 `app.include_router(xxx_api.router)` 挂载（`app/main.py:78-82`）。

现有挂载一览（真实前缀，新增端点先选一个合适的挂）：

| 模块 | prefix | 挂载位置 |
|---|---|---|
| `app/api/auth.py` | `/api/auth` | `app/main.py:78` |
| `app/api/tickets.py` | `/api/tickets` | `app/main.py:79` |
| `app/api/governance.py` | `/api`（tags=["governance"]，端点含 `/api/metrics`、`/api/approvals`、`/api/routes`、`/api/traces/{ticket_id}`） | `app/main.py:80` |
| `app/api/kb.py` | `/api/kb` | `app/main.py:81` |
| `app/api/mcp.py` | `/api/mcp` | `app/main.py:82` |

新增端点两种方式：

- **简单**：在现有 router 上加 `@router.get(...)` / `@router.post(...)`（例如把新端点挂进 `app/api/tickets.py` 或 `app/api/kb.py`）；
- **新领域**：新建 app/api/xxx.py 定义 router，并在 `app/main.py` 加一行 `include_router`（见第 2 节示例）。

## 2. 最小示例：GET 列表端点（带分页）

示例：新增 `GET /api/users`——用户列表（分页），直接复用现有 `users` 表（列：`id` / `tenant_id` / `username` / `role` / `is_active`）。

**第 1 步**：新建 app/api/users.py：

```python
"""用户列表路由（示例：分页 + RBAC）。"""
from fastapi import APIRouter, Depends

from ..deps import require_role
from ..db import session

router = APIRouter(prefix="/api/users", tags=["users"])


@router.get("")
def list_users(ctx: dict = Depends(require_role("operator", "admin")),
               page: int = 1, page_size: int = 20):
    page = max(page, 1)
    page_size = min(max(page_size, 1), 100)   # 分页钳制，防全表拉取
    with session() as conn:
        total = conn.execute(
            "SELECT COUNT(*) c FROM users WHERE tenant_id=?",
            (ctx["tenant_id"],)).fetchone()["c"]
        items = [dict(r) for r in conn.execute(
            "SELECT id, username, role, is_active FROM users"
            " WHERE tenant_id=? ORDER BY id LIMIT ? OFFSET ?",
            (ctx["tenant_id"], page_size, (page - 1) * page_size))]
    return {"items": items, "total": total, "page": page,
            "page_size": page_size, "pages": (total + page_size - 1) // page_size}
```

**第 2 步**：`app/main.py` 注册：

```python
from .api import users as users_api   # 与既有 auth/tickets/... 并列
# ...
app.include_router(users_api.router)
```

**第 3 步**：验证（curl 见第 5 节）。

> 分页响应结构 `{items, total, page, page_size, pages}` 与真实端点 `app/api/tickets.py:list_tickets` 完全同构（该端点还支持 `status`/`q` 筛选，可作更完整的模板）。

## 3. 依赖注入与 RBAC 用法

两个现成依赖都在 `app/deps.py`：

- `app/deps.py:current_user`：读取 `X-Auth-Token` Header → `app/security.py:resolve_token` → 返回 `ctx` 字典 `{user_id, username, role, tenant_id}`；未登录/会话过期抛 401；
- `app/deps.py:require_role`：`require_role("operator", "admin")` 返回一个依赖，角色不在列抛 403。

| 需求 | 写法 | 真实示例 |
|---|---|---|
| 仅需登录态 | `ctx: dict = Depends(current_user)` | `app/api/tickets.py:create_ticket` |
| 指定角色 | `ctx: dict = Depends(require_role("operator", "admin"))` | `app/api/tickets.py:list_all_tickets` |
| 租户 / 归属隔离 | 函数内比 `ctx["tenant_id"]` / `ctx["user_id"]` | `app/api/tickets.py:_ensure_access`（租户不符 403、employee 仅自己工单） |
| 入参约束 | Pydantic `BaseModel` + `Field(min_length=..., max_length=...)` | `app/api/auth.py:LoginIn` |
| 响应模型 | `response_model=Pydantic模型`，返回前 `model_dump()` | `app/api/auth.py:LoginOut` |

要点：employee 可见范围要按角色收窄——真实模式见 `app/api/tickets.py:list_tickets`（employee 时 `requester_id = ctx["user_id"]`，operator/admin 看租户全部）。

## 4. 事件 / 审计落库

**工单域端点**（与工单绑定的操作）用 `app/daos/tickets.py` 的两个函数：

- `app/daos/tickets.py:add_event`（签名 `add_event(ticket_id, event_type, payload, actor)`）——落 `ticket_events` 表，前端经 WS/事件流实时可见；真实事件类型：`created` / `user_message` / `tool_call` / `state_transition` / `confirmed` / `cancelled` / `closed`；
- `app/daos/tickets.py:transition`（签名 `transition(ticket_id, to, actor, reason="")`）——状态变更走状态机，非法跳步直接拒绝；真实用法见 `app/api/tickets.py:confirm` / `cancel` / `close`。

```python
# 真实模式（节选自 app/api/tickets.py:confirm 同构写法）
daos.transition(ticket_id, "done", ctx["username"], reason="客户确认完成")
daos.add_event(ticket_id, "confirmed", {"by": ctx["username"]}, ctx["username"])
```

**非工单域端点**（如第 2 节的 `/api/users`）：`add_event` 面向工单上下文，不适用；建议自建审计表，或至少用 `app/tracelog.py:log` / 函数级 `@trace_call("api.<module>.<fn>")` 打点（`app/tracelog.py:trace_call` 保留原签名）。`app/main.py` 已有请求级中间件记录 method + path（`app/main.py:85-89`）。

## 5. 测试与验证（curl + rbac_smoke 风格）

**curl 三步**（本地 uvicorn 跑在 `http://127.0.0.1:8000`，仓库根目录）：

```bash
# 1) 演示环境：先播种账户，再取密码（APP_ENV=production 时 seed 不生效）
curl -s -X POST http://127.0.0.1:8000/api/auth/seed
curl -s http://127.0.0.1:8000/api/auth/seed-info      # dev 返回 password

# 2) 登录拿 token（也可用 SEED_USER_PASSWORD 固定演示密码）
TOKEN=$(curl -s -X POST http://127.0.0.1:8000/api/auth/login \
  -H "Content-Type: application/json" \
  -d '{"username":"admin","password":"<seed-info 返回的密码>"}' \
  | python3 -c "import sys,json;print(json.load(sys.stdin)['token'])")

# 3) 调新端点（带 token）
curl -s "http://127.0.0.1:8000/api/users?page=1&page_size=20" -H "X-Auth-Token: $TOKEN"
```

预期：无 token → 401；`employee` → 403（`require_role("operator","admin")`）；`operator`/`admin` → 200 分页包裹。

**rbac_smoke 风格**（TestClient + 临时库，完整参照 `scripts/rbac_smoke.py`）：

```python
import os, tempfile
os.environ["APP_DB_PATH"] = os.path.join(tempfile.mkdtemp(), "t.db")
os.environ["APP_ENV"] = "development"
from fastapi.testclient import TestClient
from app.main import app

client = TestClient(app)

def login(u):
    r = client.post("/api/auth/login", json={"username": u, "password": "pass123"})
    assert r.status_code == 200
    return {"X-Auth-Token": r.json()["token"]}

# 播种三种角色用户（参照 scripts/rbac_smoke.py:23-29：INSERT + hash_password("pass123")）

h_emp, h_adm = login("employee"), login("admin")
assert client.get("/api/users", headers=h_emp).status_code == 403   # employee 越权
assert client.get("/api/users", headers=h_adm).status_code == 200   # admin 放行
assert client.get("/api/users").status_code == 401                  # 未登录
```

**文档自检**：新端点若被文档引用，`python3 scripts/check_docs.py` 会逐条校验路径与符号（`app/api/xx.py:函数` 必须真实存在）——先跑通再落文档。

## 6. 前端对接（如需要）

- API 客户端集中在 `web/lib/api.ts`：`req()` 封装 fetch、localStorage token 注入 `X-Auth-Token`、401 自动清 token 并跳 `/login`；
- 新增调用 = 在 `web/lib/api.ts` 加一个函数（参照 `listMcpServers` 的写法）；页面侧参照 `web/app/mcp/page.tsx`（`useEffect` + 轮询/刷新模式）；
- 后端地址经 `NEXT_PUBLIC_API_URL` 配置（`web/.env.local`，见 [配置清单.md](../03-部署运维/配置清单.md) 前端节）。
