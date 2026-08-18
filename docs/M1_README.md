# M1 —— 骨架（已完成）

后端可运行：FastAPI + SQLite + 登录/RBAC + 工单 CRUD + 状态机 + 最小 LOOP + SSE。

## 运行
```bash
python3 -m venv .venv
. .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env
uvicorn app.main:app --reload --port 8000
```

## 验证
```bash
. .venv/bin/activate
APP_DB_PATH=/tmp/m1.db python scripts/smoke.py     # 冒烟：建单→LOOP→done
```
HTTP 端到端：启动后
```bash
curl -X POST localhost:8000/api/auth/login -H 'Content-Type: application/json' \
  -d '{"username":"admin","password":"pass123"}'   # 返回 token
```
演示账户：`employee` / `operator` / `admin`，密码均 `pass123`。

## 已实现（对齐笔记）
- 入口：登录(token)、租户隔离(`tenant_id` 贯穿)、RBAC 三角色
- 状态机：`created→triaged→gathering→agent_running→done`，禁止跳步 + 审计事件
- 工具层：注册表白名单、参数校验、脱敏(`masker`)
- 最小 LOOP：`orchestrator` + LLM 占位后端
- SSE 事件流

## 待 M2
- 真实 LLM（OpenAI/Ollama 双后端）、模型路由
- HITL 审批、幂等键、三重预算、Auto-Compact、Trace 落库、metrics
