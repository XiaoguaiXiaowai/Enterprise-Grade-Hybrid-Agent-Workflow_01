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

    model_provider: str = os.getenv("MODEL_PROVIDER", "openrouter")
    model_name: str = os.getenv("MODEL_NAME", "qwen2.5:7b")
    # OpenAI/兼容网关（M2 遗留兼容层，SDK 模式下不再使用）
    openai_api_key: str = os.getenv("OPENAI_API_KEY", "")
    openai_base_url: str = os.getenv("OPENAI_BASE_URL", "")
    # OpenRouter（主链路，OpenAI 兼容 /v1/chat/completions）
    openrouter_api_key: str = os.getenv("OPENROUTER_API_KEY", "")
    openrouter_base_url: str = os.getenv("OPENROUTER_BASE_URL", "https://openrouter.ai/api/v1")
    # Ollama 本地（兜底链路）
    ollama_base_url: str = os.getenv("OLLAMA_BASE_URL", "http://127.0.0.1:11434/v1")
    ollama_model_name: str = os.getenv("OLLAMA_MODEL_NAME", "qwen2.5:7b")

    budget_max_steps: int = int(os.getenv("BUDGET_MAX_STEPS", "25"))
    budget_max_tokens: int = int(os.getenv("BUDGET_MAX_TOKENS", "200000"))
    budget_max_seconds: int = int(os.getenv("BUDGET_MAX_SECONDS", "600"))
    # 函数级调用日志：off | info | debug（见 tracelog.py）
    log_level: str = os.getenv("APP_LOG_LEVEL", "off")

    # 向量知识库（本地 RAG）：Ollama bge-m3 生成 embedding，LanceDB 存向量
    vector_db_path: str = os.getenv("VECTOR_DB_PATH", "data/vector-kb")
    embedding_base_url: str = os.getenv("EMBEDDING_BASE_URL", "http://127.0.0.1:11434/v1")
    embedding_model: str = os.getenv("EMBEDDING_MODEL", "bge-m3:latest")
    embedding_dim: int = int(os.getenv("EMBEDDING_DIM", "1024"))
    vector_chunk_size: int = int(os.getenv("VECTOR_CHUNK_SIZE", "300"))
    vector_chunk_overlap: int = int(os.getenv("VECTOR_CHUNK_OVERLAP", "60"))
    vector_top_k: int = int(os.getenv("VECTOR_TOP_K", "3"))

    @property
    def abs_db_path(self) -> Path:
        p = Path(self.db_path)
        return p if p.is_absolute() else (BASE_DIR / p)


settings = Settings()
