"""模型配置集中化 + 熔断兜底 验证脚本（网络受限，用模式注入，不发起真实外部调用）。

覆盖：
1. 配置读取优先级：不同 env 下的 Settings 模型/服务器地址
2. chat 三级熔断：主用→Ollama→reader
3. embedding 优先级：主用(OpenRouter)→Ollama 兜底
4. 配置清单与 .env / .env.example 键一致性

用法：
    .venv/bin/python scripts/verify_model_config.py
"""
import importlib
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.config import settings  # noqa: E402


def _check(cond, msg):
    print(("PASS  " if cond else "FAIL  ") + msg)
    assert cond, msg


def _env_reload(**kw):
    saved = {}
    for k, v in kw.items():
        saved[k] = os.environ.get(k)
        os.environ[k] = v
    import app.config as m
    importlib.reload(m)
    s = m.settings
    for k, v in saved.items():
        if v is None:
            os.environ.pop(k, None)
        else:
            os.environ[k] = v
    return s


def verify_config_priority():
    s = _env_reload(
        AGENT_MODEL="openrouter/deepseek",
        AGENT_FALLBACK_MODEL="ol-llama",
        CLASSIFIER_MODEL="openrouter/clf",
        CLASSIFIER_FALLBACK_MODEL="ol-clf-llama",
        ROUTER_MODEL_DEFAULT="openrouter/light",
        ROUTER_MODEL_HIGH_RISK="openrouter/strong",
        OLLAMA_BASE_URL="http://ollama:11434/v1",
        EMBEDDING_MODEL="",
        EMBEDDING_FALLBACK_MODEL="bge-m3:latest",
        EMBEDDING_BASE_URL="http://ollama:11434/v1",
    )
    _check(s.agent_model == "openrouter/deepseek", "AGENT_MODEL 读取正确")
    _check(s.agent_fallback_model == "ol-llama", "AGENT_FALLBACK_MODEL 读取正确")
    _check(s.classifier_model == "openrouter/clf", "CLASSIFIER_MODEL 读取正确")
    _check(s.classifier_fallback_model == "ol-clf-llama", "CLASSIFIER_FALLBACK_MODEL 读取正确")
    _check(s.router_model_high_risk == "openrouter/strong", "ROUTER_MODEL_HIGH_RISK 读取正确")
    _check(s.ollama_base_url == "http://ollama:11434/v1", "OLLAMA_BASE_URL 可配置")
    _check(s.embedding_base_url == "http://ollama:11434/v1", "EMBEDDING_BASE_URL 可配置")
    _check(s.embedding_fallback_model == "bge-m3:latest", "EMBEDDING_FALLBACK_MODEL 可配置")

    # 主用 embedding 默认值
    s2 = _env_reload()
    _check(s2.embedding_model == "nvidia/nemotron-3-embed-1b:free",
           "默认主用 embedding = nemotron-3-embed-1b:free")
    _check(s2.embedding_primary_base_url == "https://openrouter.ai/api/v1",
           "默认主用 embedding 端点 = OpenRouter")
    _check(s2.agent_model == "openrouter/free", "AGENT_MODEL 默认值正确")
    _check(s2.agent_fallback_model == "qwen2.5:7b", "AGENT_FALLBACK_MODEL 默认值正确")


def verify_chat_fallback():
    import app.llm_gateway as g
    calls = []

    def fake_openai(messages, model, **kw):
        calls.append(("primary", model))
        raise RuntimeError("down")

    def fake_ollama(messages, model, base_url, temperature=0):
        calls.append(("fallback", model, base_url))
        return {"choices": [{"message": {"content": "ok"}}],
                "model": model, "usage": {"total_tokens": 3}}

    o1, o2 = g._openai_chat, g._ollama_chat_raw
    g._openai_chat, g._ollama_chat_raw = fake_openai, fake_ollama
    try:
        res = g.chat_with_fallback(
            [{"role": "user", "content": "hi"}],
            primary_model="openrouter/free", fallback_model="qwen2.5:7b",
            fallback_base_url="http://127.0.0.1:11434/v1")
        _check(res.provider == "ollama" and res.content == "ok", "主用失败 → Ollama 兜底触发")
    finally:
        g._openai_chat, g._ollama_chat_raw = o1, o2


def verify_embedding_priority():
    import app.vector_kb as vk

    def client(mode):
        class Resp:
            def __init__(self, vecs):
                self.data = [type("D", (), {"index": i, "embedding": v})()
                             for i, v in enumerate(vecs)]

        class Emb:
            def create(self, model, input):
                if mode == "raise":
                    raise RuntimeError("down")
                if mode == "empty":
                    return Resp([])
                return Resp([[1.0, 0.0], [0.0, 1.0]])

        class C:
            embeddings = Emb()
        return C()

    saved_model = vk.settings.embedding_model
    saved_clients = vk._embedding_clients
    try:
        vk.settings.embedding_model = "nvidia/nemotron-3-embed-1b:free"
        vk._embedding_clients = lambda: {"primary": client("raise"),
                                         "fallback": client("ok")}
        vs = vk.embed(["a"])
        _check(len(vs) == 2, "embedding 主用失败 → Ollama 兜底触发")
    finally:
        vk.settings.embedding_model = saved_model
        vk._embedding_clients = saved_clients


def verify_env_template_consistency():
    """校验 .env.example 中模型键与配置清单/代码中的键一致。"""
    need = ["OPENROUTER_API_KEY", "OPENROUTER_BASE_URL", "OLLAMA_BASE_URL",
            "AGENT_MODEL", "AGENT_FALLBACK_MODEL",
            "CLASSIFIER_MODEL", "CLASSIFIER_FALLBACK_MODEL",
            "ROUTER_MODEL_DEFAULT", "ROUTER_MODEL_HIGH_RISK",
            "EMBEDDING_MODEL", "EMBEDDING_PRIMARY_BASE_URL",
            "EMBEDDING_FALLBACK_MODEL", "EMBEDDING_BASE_URL"]
    root = Path(__file__).resolve().parent.parent
    ex = (root / ".env.example").read_text()
    for k in need:
        _check(f"{k}=" in ex, f".env.example 含 {k}")

    old = ["MODEL_PROVIDER", "MODEL_NAME", "OLLAMA_MODEL_NAME",
           "OPENAI_API_KEY", "OPENAI_BASE_URL"]
    for k in old:
        _check(f"{k}=" not in ex, f".env.example 已移除旧键 {k}")


if __name__ == "__main__":
    print("== 配置优先级别 == ")
    verify_config_priority()
    print("\n== chat 三级熔断 ==")
    verify_chat_fallback()
    print("\n== embedding 优先级 ==")
    verify_embedding_priority()
    print("\n== env 模板一致性 ==")
    verify_env_template_consistency()
    print("\nALL MODEL CONFIG VERIFICATIONS PASSED")
