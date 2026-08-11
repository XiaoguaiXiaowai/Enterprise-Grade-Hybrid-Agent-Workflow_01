# 企业级混合 Agent 工作流 Demo

> **一个企业 IT 工单智能处理系统，展示"企业级 Agent Harness"的 8 大工程能力。**
> 它不是演示 AI 多聪明，而是演示 **AI 如何被可靠、可控、可审计地接进企业业务**。

- 详细开发文档：请见 [`doc/开发文档.md`](doc/开发文档.md)
- 学习笔记原文：[`doc/学习笔记.md`](doc/学习笔记.md)

---

## 开场 30 秒：项目定位

```text
员工提交 IT 工单（查权限 / 开通账号 / 排查故障 / 改配置）
        │
        ▼
  企业级 Agent Harness（自研，非 LangGraph）
        │  —— 可靠：状态机约束、幂等、预算、停止条件
        │  —— 可控：IAM 最小权限、工具白名单、HITL 审批
        │  —— 可审计：Trace 全链路回放、监控指标
        ▼
  关闭工单并交付（审批通过 / 自动收敛 / 归档存档）
```

**一句话讲稿**：我把"单体大模型应用"拆成了**受控规则层（Harness）+ 智能层（LLM）**，让大模型只负责判断，把执行权交给确定性的流程与审批。

---

## 系统架构图

```mermaid
flowchart TB
    subgraph Client["客户端"]
        emp[员工 Browser]
        adm[管理员/审批人 Browser]
    end

    subgraph entry["入口层 · FastAPI / Nginx (HTTPS)"]
        auth[登录 / 会话 / 租户 / RBAC]
        risk[输入风险分级 low·mid·high]
        api[工单 API + SSE 事件流]
        mon[监控看板 + Trace 回放]
    end

    subgraph harness["Agent Harness 内核（自研 Python）"]
        orch[编排器 orchestrator<br/>状态机 · LOOP]
        cta[上下文装配 context_assembler<br/>选 / 压 / 截 + 结构化]
        router[模型路由 model_router]
        llm[LLM 网关<br/>OpenAI SDK ⇄ Ollama]
        guard[守卫 guards<br/>重试 · 停止 · 重复检测]
        budget[预算 budget<br/>步数 / Token / 时间]
        skills[工具层 MPC-Skill<br/>白名单 · 参数校验 · 脱敏 · HITL · 幂等]
        hello[四项安全约束<br/>IAM凭证 / Schema校验 / 幂等状态机 / HITL]
    end

    subgraph memory["记忆五分层"]
        sess[Session]
        task[Task State]
        memo[Memory 长期]
        art[Artifact 产物]
        trc[Trace 审计]
    end

    subgraph data["数据层 · 本地零成本"]
        sql[(SQLite<br/>多租户 · RBAC · 指标)]
        fs[(文件系统<br/>artifacts · traces · memory)]
        kb[(知识库<br/>RAG / FTS)]
    end

    emp --> auth
    adm --> auth
    auth --> risk
    risk --> api
    api --> orch
    orch --> cta --> llm
    router --> llm
    orch --> guard --> budget
    orch --> skills --> hello
    skills --> memory
    orch --> memory
    memory --> data
    data --> sql
    data --> fs
    data --> kb
    trc --> mon
    sql --> mon
```

---

## 工单状态机（确定性流程约束）

```mermaid
stateDiagram-v2
    [*] --> created : 员工建单（结构化）
    created --> triaged : 意图分类 + 风险分级
    triaged --> gathering : 装配上下文 / RAG
    triaged --> failed : 分级失败
    gathering --> agent_running : LOOP 执行
    gathering --> failed
    agent_running --> gathering : 需要更多信息
    agent_running --> awaiting_approval : 高风险工具 → HITL 中断
    agent_running --> done : 收敛 done:true + 校验
    agent_running --> failed
    agent_running --> cancelled : 预算/超时/停止条件
    awaiting_approval --> running : 审批通过 → 恢复执行
    awaiting_approval --> cancelled : 驳回
    awaiting_approval --> gathering : 改方案
    running --> done : 执行完成
    running --> failed
    running --> cancelled
    done --> archived : 任务状态归档（只读）
    failed --> archived
    cancelled --> archived
```

---

## 8 大能力 → Demo 映射图

```mermaid
mindmap
  root((企业级 Harness))
    流程定义
      目标清晰化
      隐性知识沉淀
      Agent vs Workflow
      风险边界
    业务入口
      鉴权 PW/SSO
      租户隔离
      IAM 权限
      输入安全分级
    核心 LOOP
      上下文装配 选压截
      模型路由
      四层安全约束
      工具调用纪律
      记忆五分层
      异常处理
      三重预算
    业务出口
      审批 HITL
      收敛校验
      交付归档
    工具层 MPC-Skill
      最小权限
      白名单
      参数校验
      脱敏
      HITL 审批
      幂等重试补偿
      审计日志
    Trace
      模型 / 提示词版本
      上下文来源
      工具 · 状态变化
      Token / 延迟 / 重试
   监控评估
      成功率
      正确失败率
      延迟 / 成本
      人工接管率
```

---

## 技术栈速览

| 层 | 选型 | 为什么 |
|---|---|---|
| 后端 | Python + FastAPI | 轻量、结构化、面试可逐行讲解 |
| Workflow | 自研状态机 | 不用 LangGraph，快照/HITL 原理可控可讲 |
| LLM | OpenAI SDK ⇄ Ollama | 统一 `/v1/chat/completions`，一个开关切换 |
| 存储 | SQLite + 文件系统 | 单机、0 成本、状态外部化 |
| 前端 | Next.js (App Router) | 贴近真实企业栈，与 FastAPI 解耦，面试加分 |
| 部署 | Docker Compose + Nginx + HTTPS | 阿里云 Ubuntu 单机零成本上线 |

---

## 演示路径（面试 10 分钟版）

1. 建一张"开通 DB 只读账号"的高风险工单 → 展示风险分级 + 权限收敛
2. 看 Agent LOOP：上下文装配（选/压/截）、工具白名单、脱敏
3. 触发 HITL：工单进入审批 → 审批台批准 → 恢复执行
4. 打开 Trace 时间线：模型版本 / 提示词版本 / 工具调用 / 状态转移一屏可见
5. 打开 `/metrics`：成功率、正确失败率、延迟、成本、人工接管率
6. 收尾：讲"切 Ollama / 走 SSO / 上 K8s 怎么做"——展示可演进性
