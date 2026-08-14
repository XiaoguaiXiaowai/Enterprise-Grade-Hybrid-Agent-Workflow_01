# M2 —— Harness 内核（完成）

在 M1 骨架之上实现了企业级 Harness 的核心工程能力，全部对齐学习笔记。

## 新增能力
- **真实 LLM 双引擎**：`app/llm_gateway.py` 支持 OpenAI SDK / Ollama（统一 `/v1/chat/completions`），无密钥/离线自动回退 `reader`（可演示/可测）
- **模型路由**：`app/model_router.py` 按意图+风险选模型；高危升格强模型
- **上下文装配**：`app/context_assembler.py` 选/压/截 + 结构化 `<goal><state><history>...` 防自我污染 + Auto-Compact
- **HITL 审批流**：高风险工具触发 `awaiting_approval` → 审批通过后 `resume_ticket` 恢复执行（防重复申请）
- **幂等键 + 重复检测**：`app/guards.py` + `tool_calls` 表，同参数时间窗内已成功即拦截
- **三重预算**：`app/budget.py` 步数/Token/时间，触顶转 `failed(budget)`
- **Trace**：`app/trace.py` 模型/提示词版本/上下文来源/工具/状态/延迟/重试全落库，`/api/traces/{id}` 回放
- **监控指标**：`app/metrics.py` 成功率/正确失败率/延迟/成本/人工接管率，`/api/metrics`
- **新工具**：`grant_db_readonly`（高危，需审批+幂等）、`revoke_db_readonly`（补偿）

## 模型配置
```bash
# .env（注：M2 期旧键 MODEL_PROVIDER/MODEL_NAME/OPENAI_* 已由 M5 重构移除，统一为新场景键）
OPENROUTER_API_KEY=sk-or-v1-...
OPENROUTER_BASE_URL=https://openrouter.ai/api/v1
OLLAMA_BASE_URL=http://127.0.0.1:11434/v1
AGENT_MODEL=openrouter/free
AGENT_FALLBACK_MODEL=qwen2.5:7b
BUDGET_MAX_STEPS=25
BUDGET_MAX_TOKENS=200000
BUDGET_MAX_SECONDS=600
```

## 验证
```bash
. .venv/bin/activate
rm -f /tmp/m2.db*
python scripts/smoke_m2.py        # 低风险 + 高风险(HITL) + Trace + 指标
```
HTTP 端到端（高风险 HITL 全流程）见 `scripts/` 或手动：
启动后，admin 建高风险单 → run → 返回 `awaiting_approval` → operator 调 `POST /api/tickets/{id}/approve` → 恢复执行到 `done`。

## 演示账户
`employee` / `operator` / `admin`，密码均 `pass123`。

## 待 M3
- Next.js 前端（工单台/审批台/Trace/指标）
- 知识库 RAG、演示数据 `seed_demo.py`
- HTTPS + Nginx + Docker Compose 完整部署、面试话术
