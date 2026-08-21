"""应用配置：从环境变量 / .env 读取。"""
import os
from pathlib import Path

try:
    from dotenv import load_dotenv
    load_dotenv()
except Exception:
    pass

BASE_DIR = Path(__file__).resolve().parent.parent


class Settings:
    env: str = os.getenv("APP_ENV", "development")
    db_path: str = os.getenv("APP_DB_PATH", "data/app.db")
    secret_key: str = os.getenv("SECRET_KEY", "dev-insecure-key")

    # 主用提供方（OpenRouter，OpenAI 兼容 /v1/chat/completions）
    openrouter_api_key: str = os.getenv("OPENROUTER_API_KEY", "")
    openrouter_base_url: str = os.getenv("OPENROUTER_BASE_URL", "https://openrouter.ai/api/v1")
    # Ollama 通用服务器地址（可配置，Docker 内网用 http://ollama:11434/v1）
    ollama_base_url: str = os.getenv("OLLAMA_BASE_URL", "http://127.0.0.1:11434/v1")

    # 场景一：Agent 编排 / 计划执行（OpenAI Agents SDK 主链路 + 传统网关）
    #agent_model: str = os.getenv("AGENT_MODEL", "openrouter/free")
    agent_model: str = os.getenv("AGENT_MODEL", "")
    agent_fallback_model: str = os.getenv("AGENT_FALLBACK_MODEL", "")

    # 场景二：工单意图/风险分类
    #classifier_model: str = os.getenv("CLASSIFIER_MODEL", "openrouter/free")
    classifier_model: str = os.getenv("CLASSIFIER_MODEL", "")
    classifier_fallback_model: str = os.getenv("CLASSIFIER_FALLBACK_MODEL", "")

    # 路由默认模型（model_router 在未显式/按意图给默认时使用）
    #router_model_default: str = os.getenv("ROUTER_MODEL_DEFAULT", "openrouter/free")
    router_model_default: str = os.getenv("ROUTER_MODEL_DEFAULT", "")
    #router_model_high_risk: str = os.getenv("ROUTER_MODEL_HIGH_RISK", "openrouter/free")
    router_model_high_risk: str = os.getenv("ROUTER_MODEL_HIGH_RISK", "")

    budget_max_steps: int = int(os.getenv("BUDGET_MAX_STEPS", "25"))
    budget_max_tokens: int = int(os.getenv("BUDGET_MAX_TOKENS", "200000"))
    budget_max_seconds: int = int(os.getenv("BUDGET_MAX_SECONDS", "600"))
    # 函数级调用日志：off | info | debug（见 tracelog.py）
    log_level: str = os.getenv("APP_LOG_LEVEL", "off")

    # ===== 架构改善阶段1：Agent 后台任务执行器 =====
    # run/追问/审批入队即返回，由 worker 线程池执行（避免同步占用 HTTP 线程）
    agent_workers: int = int(os.getenv("AGENT_WORKERS", "4"))
    agent_llm_concurrency: int = int(os.getenv("AGENT_LLM_CONCURRENCY", "4"))
    agent_llm_timeout: int = int(os.getenv("AGENT_LLM_TIMEOUT", "60"))

    # ===== 整改②：LLM 调用参数（魔法数字外部化）=====
    llm_temperature: float = float(os.getenv("LLM_TEMPERATURE", "0"))
    # 注意：max_tokens 是「含推理 token 的总输出预算」。若主用推理类模型（如 liquid/*）
    # 需调大，否则思考阶段即耗尽预算、返回空 content；512 对非推理模型足够且快速。
    llm_max_tokens_classify: int = int(os.getenv("LLM_MAX_TOKENS_CLASSIFY", "512"))
    llm_max_tokens_rewrite: int = int(os.getenv("LLM_MAX_TOKENS_REWRITE", "512"))
    # 成本估算单价（美元/百万 token）；默认 0=未定价，配置后 /metrics 的 cost 为真实估算
    llm_cost_per_1m_tokens: float = float(os.getenv("LLM_COST_PER_1M_TOKENS", "0"))

    # ===== 整改②：上下文装配 / 守卫阈值 =====
    ctx_max_history: int = int(os.getenv("CTX_MAX_HISTORY", "8"))
    ctx_max_obs_len: int = int(os.getenv("CTX_MAX_OBS_LEN", "1200"))
    ctx_min_truncate: int = int(os.getenv("CTX_MIN_TRUNCATE", "30"))
    dedup_window_minutes: int = int(os.getenv("DEDUP_WINDOW_MINUTES", "5"))
    # 长 observation 用 LLM 摘要压缩（默认关，零额外成本；开启后 LLM 失败自动回退截断）
    ctx_llm_compress: bool = os.getenv(
        "CTX_LLM_COMPRESS", "false").lower() in ("1", "true", "yes", "on")

    # ===== 整改②：安全参数 =====
    pbkdf2_iterations: int = int(os.getenv("PBKDF2_ITERATIONS", "100000"))
    session_ttl_hours: int = int(os.getenv("SESSION_TTL_HOURS", "12"))
    session_token_bytes: int = int(os.getenv("SESSION_TOKEN_BYTES", "32"))

    # ===== 整改⑦：函数级日志目录（相对项目根或绝对路径）=====
    log_dir: str = os.getenv("LOG_DIR", "logs")

    # ===== RAG 检索增强（整改⑧）=====
    # 重排序（rerank）：主用 OpenRouter /api/v1/rerank，兜底 Ollama（探测 /api/rerank，不可用自动降级）
    rerank_enabled: bool = os.getenv("RERANK_ENABLED", "true").lower() in ("1", "true", "yes", "on")
    rerank_model: str = os.getenv(
        "RERANK_MODEL", "nvidia/llama-nemotron-rerank-vl-1b-v2:free")
    rerank_base_url: str = os.getenv(
        "RERANK_BASE_URL", "https://openrouter.ai/api/v1")
    # 兜底：Ollama rerank（bce-reranker-base_v1 等）；Ollama 无原生 /api/rerank 时静默降级为不精排
    rerank_fallback_model: str = os.getenv(
        "RERANK_FALLBACK_MODEL", "bce-reranker-base_v1")
    rerank_fallback_base_url: str = os.getenv(
        "RERANK_FALLBACK_BASE_URL", "http://127.0.0.1:11434")
    # 精排候选数：召回先放大到 top_k × 该倍数，再 rerank 取前 top_k
    rerank_recall_factor: int = int(os.getenv("RERANK_RECALL_FACTOR", "4"))

    # 查询改写（检索前先让 LLM 把口语化说法改写为利于检索的关键词；默认关，涉及额外 LLM 调用）
    query_rewrite_enabled: bool = os.getenv(
        "RAG_QUERY_REWRITE_ENABLED", "false").lower() in ("1", "true", "yes", "on")
    # 动态加权 RRF / 意图路由（按查询特征调整向量 vs 关键词权重；默认关保持原行为）
    adaptive_rerank_weight: bool = os.getenv(
        "RAG_ADAPTIVE_WEIGHT", "false").lower() in ("1", "true", "yes", "on")
    # 检索结果是否携带父块文本（small-to-big：命中子块时带回所属大块，供上下文更完整）
    return_parent_chunk: bool = os.getenv(
        "RAG_RETURN_PARENT", "false").lower() in ("1", "true", "yes", "on")

    # ===== RAG 检索增强（整改⑧-续）：上下文窗口 / 多路召回 / 模糊追问 =====
    # ④ 上下文窗口扩展：命中块前后各拼接 N 个邻居块（0=关，保持现状单块返回）
    rag_context_window: int = int(os.getenv("RAG_CONTEXT_WINDOW", "1"))
    # ② 多路召回（多 query 变体并行）：默认关（与 query_rewrite 同成本考量）
    rag_multi_query_enabled: bool = os.getenv(
        "RAG_MULTI_QUERY_ENABLED", "false").lower() in ("1", "true", "yes", "on")
    # ② 多路召回中是否启用 LLM 拆分子问题（额外调用，仅多路开启时有意义）
    rag_multi_query_subsplit: bool = os.getenv(
        "RAG_MULTI_QUERY_SUBSPLIT", "false").lower() in ("1", "true", "yes", "on")
    # ② 多路召回 query 变体数量上限（防变体爆炸）
    rag_multi_query_max_variants: int = int(os.getenv("RAG_MULTI_QUERY_MAX_VARIANTS", "4"))
    # ① 模糊追问：top1 命中置信度（rerank 分数）低于此值 → 触发澄清信号；0=关闭模糊判定
    rag_clarify_threshold: float = float(os.getenv("RAG_CLARIFY_THRESHOLD", "0.05"))
    # ① 澄清信号总开关
    rag_clarify_enabled: bool = os.getenv(
        "RAG_CLARIFY_ENABLED", "true").lower() in ("1", "true", "yes", "on")

    # 向量知识库（本地 RAG）：主用 OpenRouter embedding，兜底 Ollama bge-m3，LanceDB 存向量
    vector_db_path: str = os.getenv("VECTOR_DB_PATH", "data/vector-kb")
    # 主用 embedding（OpenRouter）：默认 nvidia/nemotron-3-embed-1b:free，端点独立可配
    embedding_model: str = os.getenv("EMBEDDING_MODEL", "nvidia/nemotron-3-embed-1b:free")
    embedding_primary_base_url: str = os.getenv(
        "EMBEDDING_PRIMARY_BASE_URL", "https://openrouter.ai/api/v1")
    # 兜底 embedding（Ollama）：复用 EMBEDDING_BASE_URL 端点
    embedding_fallback_model: str = os.getenv("EMBEDDING_FALLBACK_MODEL", "bge-m3:latest")
    embedding_base_url: str = os.getenv("EMBEDDING_BASE_URL", "http://127.0.0.1:11434/v1")
    embedding_dim: int = int(os.getenv("EMBEDDING_DIM", "1024"))
    vector_chunk_size: int = int(os.getenv("VECTOR_CHUNK_SIZE", "500"))
    vector_chunk_overlap: int = int(os.getenv("VECTOR_CHUNK_OVERLAP", "200"))
    vector_top_k: int = int(os.getenv("VECTOR_TOP_K", "3"))
    # 小到大人块尺寸（small-to-big）：子块按 VECTOR_CHUNK_SIZE，父块按此尺寸；0 = 整篇文档作父块
    vector_parent_chunk_size: int = int(os.getenv("VECTOR_PARENT_CHUNK_SIZE", "1500"))
    # ANN 索引开关 + 参数（小库用精确扫描更快更准，默认关；数据量大再开，如 IVF_HNSW_SQ）
    vector_ann_enabled: bool = os.getenv(
        "VECTOR_ANN_ENABLED", "false").lower() in ("1", "true", "yes", "on")
    vector_ann_index_type: str = os.getenv("VECTOR_ANN_INDEX_TYPE", "IVF_PQ")
    vector_ann_metric: str = os.getenv("VECTOR_ANN_METRIC", "cosine")
    # 维度不一致时告警并拒绝写入（防 embedding 模型切换导致索引损坏）
    vector_embed_dim_check: bool = os.getenv(
        "VECTOR_EMBED_DIM_CHECK", "true").lower() in ("1", "true", "yes", "on")

    # ===== 建单输入校验 / 提示词重写（M5）=====
    # 建单时先用 LLM 确认输入与 IT 工单领域是否相关（默认开）
    input_relevance_check: bool = os.getenv("INPUT_RELEVANCE_CHECK", "true").lower() in ("1", "true", "yes", "on")
    # 建单内容不直接并入提示词，先用 LLM 总结归纳后再并入（默认开）
    prompt_rewrite: bool = os.getenv("PROMPT_REWRITE", "true").lower() in ("1", "true", "yes", "on")

    # ===== MCP 集成（M5）=====
    # 总开关：false 时 MCP 工具不装配、管理端点返回空列表，行为回到 M4
    mcp_enabled: bool = os.getenv("MCP_ENABLED", "true").lower() in ("1", "true", "yes", "on")
    # mcp_servers.json 路径（相对项目根或绝对路径）
    mcp_config_path: str = os.getenv("MCP_CONFIG_PATH", "mcp_servers.json")
    # 单次 MCP 操作（连接/工具调用）的超时（秒）；ServerConfig 可 per-server 覆盖
    mcp_timeout_seconds: float = float(os.getenv("MCP_TIMEOUT_SECONDS", "15"))
    @staticmethod
    def _first_nonempty(*keys: str, default: str = "") -> str:
        """取第一个非空环境变量；全部为空/未设时返回 default。"""
        for k in keys:
            v = os.getenv(k)
            if v:
                return v
        return default

    # 相关性校验专用模型（为空则复用 classifier 模型）
    relevance_model: str = _first_nonempty(
        "RELEVANCE_MODEL", "CLASSIFIER_MODEL", default="")
    relevance_fallback_model: str = _first_nonempty(
        "RELEVANCE_FALLBACK_MODEL", "CLASSIFIER_FALLBACK_MODEL", default="")
    # 提示词重写专用模型（为空则复用 classifier 模型）
    rewrite_model: str = _first_nonempty(
        "REWRITE_MODEL", "CLASSIFIER_MODEL", default="")
    rewrite_fallback_model: str = _first_nonempty(
        "REWRITE_FALLBACK_MODEL", "CLASSIFIER_FALLBACK_MODEL", default="")

    @property
    def abs_db_path(self) -> Path:
        p = Path(self.db_path)
        return p if p.is_absolute() else (BASE_DIR / p)


settings = Settings()
