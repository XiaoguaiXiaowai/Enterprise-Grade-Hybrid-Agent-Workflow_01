# M3 —— 前端 / 知识库 / 部署（完成）

## 新增能力
- **Next.js 14 前端**（`web/`，App Router，TypeScript，客户端 SPA）：
  - `/tickets` 工单台：建单（风险/意图）、列表、运行、实时 SSE 事件流
  - `/approve` 审批台（HITL）：自动轮询待审批单，通过/驳回
  - `/traces/[id]`：Trace 全链路回放
  - `/metrics`：成功率/正确失败率/延迟/成本/人工接管率看板
  - `/login` 登录页；token 存 localStorage
- **RAG 知识库**：`app/kb.py` SQLite FTS5 + 关键词兜底；`search_kb` 工具接真实检索；启动自动种内置文档
- **演示数据**：`scripts/seed_demo.py` 幂等造账密/知识库/示例工单

## 前端本地开发 / 构建
```bash
cd web
npm install
cp .env.example .env.local   # NEXT_PUBLIC_API_URL=http://localhost:8000
npm run dev                  # 开发
npm run build                # 生产构建（含类型检查）
```

## 部署（单机，Docker Compose）
```bash
cp .env.example .env          # 配置模型 provider
docker compose up -d --build
```
- 架构：Nginx :80（唯一入口）→ `/` 到 `web:3000`(Next.js)，`/api` 到 `app:8000`(FastAPI)
- SSE 走 `/api` 反代（已关 buffering）
- 可选 Ollama：启用 `docker-compose.yml` 里注释的 ollama 服务，并设 `OLLAMA_BASE_URL=http://ollama:11434/v1`

## 端到端验证
```bash
. .venv/bin/activate
rm -f /tmp/m3_e2e.db*
APP_DB_PATH=/tmp/m3_e2e.db python scripts/seed_demo.py
# 然后起服务，前端打开 /login，admin 登录 → 工单台建高风险单 → 运行
# → 审批台 operator 通过 → 查看 Trace / metrics
```

## 演示账户
`admin` / `operator` / `employee`，密码均 `pass123`。

## 全链路线索（面试讲解）
入口(登录/RBAC/租户) → 建单(风险分级) → LOOP(上下文装配/模型路由/工具/HITL/预算) → 审批(人工接管) → Trace(版本/状态/延迟) → metrics(成功率/接管率)。
