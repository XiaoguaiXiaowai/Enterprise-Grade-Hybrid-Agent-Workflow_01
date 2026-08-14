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
    agent_fallback_model: str = os.getenv("AGENT_FALLBACK_MODEL", "qwen2.5:7b")

    # 场景二：工单意图/风险分类
    #classifier_model: str = os.getenv("CLASSIFIER_MODEL", "openrouter/free")
    classifier_model: str = os.getenv("CLASSIFIER_MODEL", "")
    classifier_fallback_model: str = os.getenv("CLASSIFIER_FALLBACK_MODEL", "qwen2.5:7b")

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

    # ===== 建单输入校验 / 提示词重写（M5）=====
    # 建单时先用 LLM 确认输入与 IT 工单领域是否相关（默认开）
    input_relevance_check: bool = os.getenv("INPUT_RELEVANCE_CHECK", "true").lower() in ("1", "true", "yes", "on")
    # 建单内容不直接并入提示词，先用 LLM 总结归纳后再并入（默认开）
    prompt_rewrite: bool = os.getenv("PROMPT_REWRITE", "true").lower() in ("1", "true", "yes", "on")
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
