"""模型配置集中化 + 熔断兜底改造的验证测试。

覆盖：
- chat 主用(OpenRouter)→Ollama→reader 三级熔断触发
- embedding 主用(OpenRouter)→Ollama 兜底优先级
- 不同 env 配置下 Settings 模型与服务器地址读取正确
- classifier 主/兜底失败后回退关键词规则

可直接运行：.venv/bin/python tests/test_model_config.py
（未安装 pytest 时同样可用；安装了 pytest 亦可收集。）
"""
from __future__ import annotations

import os
import sys
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import app.config as cfg_mod  # noqa: E402
from app.llm_gateway import LLMResult  # noqa: E402

# 各模块共同持有的 settings 单例（直接改其属性，可视化反映到所有调用点）
S = cfg_mod.settings

_SAVED = {}


def _env(**kw):
    """设置环境变量并重载 app.config 模块，返回其 settings 单例（验证 env 解析）。"""
    _SAVED.clear()
    for k, v in kw.items():
        _SAVED[k] = os.environ.get(k)
        os.environ[k] = v
    import importlib
    importlib.reload(cfg_mod)
    fresh = cfg_mod.settings
    for k, v in _SAVED.items():
        if v is None:
            os.environ.pop(k, None)
        else:
            os.environ[k] = v
    _SAVED.clear()
    # reload 后旧单例已被新实例替换；各模块持有的引用仍是旧对象，仅此测试直接读 cfg_mod.settings
    return fresh


def _patch(**kw):
    """在这些调用点可见的 settings 单例上临时覆盖属性（用完恢复）。"""
    saved = {k: getattr(S, k) for k in kw if hasattr(S, k)}
    for k, v in kw.items():
        setattr(S, k, v)
    return saved


def _restore(saved):
    for k, v in saved.items():
        setattr(S, k, v)


def _patch_embedding_clients(factory):
    import app.vector_kb as vk
    orig = vk._embedding_clients
    vk._embedding_clients = factory
    return orig


def _patch_llm_backends(openai_fn, ollama_fn):
    import app.llm_gateway as g
    o1, o2 = g._openai_chat, g._ollama_chat_raw
    if openai_fn is not None:
        g._openai_chat = openai_fn
    if ollama_fn is not None:
        g._ollama_chat_raw = ollama_fn
    return o1, o2


def test_settings_env_priority():
    """不同 env 配置下 Settings 模型与服务器地址读取正确；未设自定义 embedding 时默认 nemotron。"""
    s = _env(
        AGENT_MODEL="openrouter/deepseek-v3",
        AGENT_FALLBACK_MODEL="llama3:8b",
        CLASSIFIER_MODEL="openrouter/gpt-4o-mini",
        CLASSIFIER_FALLBACK_MODEL="qwen2.5:3b",
        ROUTER_MODEL_DEFAULT="openrouter/light",
        ROUTER_MODEL_HIGH_RISK="openrouter/strong",
        OLLAMA_BASE_URL="http://ollama.internal:11434/v1",
        EMBEDDING_MODEL="",               # 未配置有效主模型 → 仅当设置时才使用
        EMBEDDING_PRIMARY_BASE_URL="https://openrouter.ai/api/v1",
        EMBEDDING_FALLBACK_MODEL="nomic-embed-text:latest",
        EMBEDDING_BASE_URL="http://ollama.internal:11434/v1",
    )
    assert s.agent_model == "openrouter/deepseek-v3"
    assert s.agent_fallback_model == "llama3:8b"
    assert s.classifier_model == "openrouter/gpt-4o-mini"
    assert s.classifier_fallback_model == "qwen2.5:3b"
    assert s.router_model_default == "openrouter/light"
    assert s.router_model_high_risk == "openrouter/strong"
    assert s.ollama_base_url == "http://ollama.internal:11434/v1"
    assert s.embedding_fallback_model == "nomic-embed-text:latest"
    assert s.embedding_base_url == "http://ollama.internal:11434/v1"

    # 默认值：主用 embedding 应为 OpenRouter nemotron，Ollama 端点用默认本地地址
    s2 = _env()  # 无任何覆盖 → 全部默认
    assert s2.embedding_model == "nvidia/nemotron-3-embed-1b:free"
    assert s2.embedding_primary_base_url == "https://openrouter.ai/api/v1"
    assert s2.agent_model == "openrouter/free"
    assert s2.agent_fallback_model == "qwen2.5:7b"
    assert s2.ollama_base_url == "http://127.0.0.1:11434/v1"


def test_chat_fallback_primary_failure():
    """主用失败 → 自动切换 Ollama 兜底（含服务器地址）。"""
    import app.llm_gateway as g

    calls = []

    def fake_openai(messages, model, **kw):
        calls.append(("primary", model))
        raise RuntimeError("primary down")

    def fake_ollama(messages, model, base_url, temperature=0):
        calls.append(("fallback", model, base_url))
        return {"choices": [{"message": {"content": "ok"}}],
                "model": model, "usage": {"total_tokens": 5}}

    o1, o2 = _patch_llm_backends(fake_openai, fake_ollama)
    try:
        res = g.chat_with_fallback(
            [{"role": "user", "content": "hi"}],
            primary_model="openrouter/free", fallback_model="qwen2.5:7b",
            fallback_base_url="http://127.0.0.1:11434/v1",
        )
        assert res.provider == "ollama", res
        assert res.content == "ok"
        assert calls == [("primary", "openrouter/free"),
                         ("fallback", "qwen2.5:7b", "http://127.0.0.1:11434/v1")]
    finally:
        g._openai_chat, g._ollama_chat_raw = o1, o2


def test_chat_fallback_all_fail_reader():
    """主用与 Ollama 都失败 → 落到 reader。"""
    import app.llm_gateway as g

    def fake_openai(messages, model, **kw):
        raise RuntimeError("primary down")

    def fake_ollama(messages, model, base_url, temperature=0):
        raise RuntimeError("ollama down")

    o1, o2 = _patch_llm_backends(fake_openai, fake_ollama)
    try:
        res = g.chat_with_fallback(
            [{"role": "user", "content": "<goal>排查VPN</goal>"}],
            primary_model="openrouter/free", fallback_model="qwen2.5:7b",
            fallback_base_url="http://127.0.0.1:11434/v1",
        )
        assert res.provider == "reader", res
    finally:
        g._openai_chat, g._ollama_chat_raw = o1, o2


def test_chat_fallback_raise_if_reader():
    """raise_if_reader=True 时均失败抛异常（classifier 据此回退规则）。"""
    import app.llm_gateway as g

    def fake_openai(messages, model, **kw):
        raise RuntimeError("primary down")

    def fake_ollama(messages, model, base_url, temperature=0):
        raise RuntimeError("ollama down")

    o1, o2 = _patch_llm_backends(fake_openai, fake_ollama)
    try:
        try:
            g.chat_with_fallback(
                [{"role": "user", "content": "hi"}],
                primary_model="openrouter/free", fallback_model="qwen2.5:7b",
                fallback_base_url="http://127.0.0.1:11434/v1",
                raise_if_reader=True,
            )
            raise AssertionError("expected RuntimeError")
        except RuntimeError:
            pass
    finally:
        g._openai_chat, g._ollama_chat_raw = o1, o2


def test_classifier_fallback_to_rules():
    """LLM 主/兜底均失败 → 回退关键词规则，source=rule。"""
    import app.classifier as clf

    saved = _patch(openrouter_api_key="sk-test")
    orig_fb = clf.chat_with_fallback

    def broken(*a, **kw):
        raise RuntimeError("all backends down")

    clf.chat_with_fallback = broken
    try:
        r = clf.classify("VPN 连不上如何排查", "无法连 VPN，给排查步骤")
        assert r["source"] == "rule", r
        assert r["intent_type"] == "troubleshoot", r
    finally:
        clf.chat_with_fallback = orig_fb
        _restore(saved)


def test_classifier_llm_path():
    """LLM 返回有效 JSON → 走 llm 源。"""
    import app.classifier as clf

    saved = _patch(openrouter_api_key="sk-test")
    orig_fb = clf.chat_with_fallback

    def fake_fb(messages, **kw):
        return LLMResult(
            content='{"intent_type":"change","risk_level":"high","reason":"授权"}',
            model="openrouter/free", provider="openrouter", usage={})

    clf.chat_with_fallback = fake_fb
    try:
        r = clf.classify("给 u-1024 开通只读", "开通 db 只读权限")
        assert r["source"] == "llm", r
        assert r["intent_type"] == "change" and r["risk_level"] == "high", r
    finally:
        clf.chat_with_fallback = orig_fb
        _restore(saved)


def test_embedding_priority():
    """embedding 主用失败/空向量 → 切换 Ollama 兜底；均失败抛异常；未配主模型直接兜底。"""
    import app.vector_kb as vk

    class _Resp:
        def __init__(self, vecs):
            self.data = [type("D", (), {"index": i, "embedding": v})()
                         for i, v in enumerate(vecs)]

    class _Client:
        def __init__(self, mode):
            self.mode = mode

        @property
        def embeddings(self):
            class _Emb:
                def create(self, model, input):
                    if self._c.mode == "raise":
                        raise RuntimeError("down")
                    if self._c.mode == "empty":
                        return _Resp([])
                    return _Resp([[1.0, 0.0], [0.0, 1.0]])
            _Emb._c = self
            return _Emb()

    saved = _patch(embedding_model="nvidia/nemotron-3-embed-1b:free",
                   embedding_fallback_model="bge-m3:latest")
    orig_clients = vk._embedding_clients
    try:
        # 1) 主用正常 → 用主用结果
        vk._embedding_clients = lambda: {"primary": _Client("ok"), "fallback": _Client("ok")}
        vs = vk.embed(["a", "b"])
        assert len(vs) == 2 and vs[0] == [1.0, 0.0], vs

        # 2) 主用失败 → 兜底
        vk._embedding_clients = lambda: {"primary": _Client("raise"), "fallback": _Client("ok")}
        vs = vk.embed(["a", "b"])
        assert len(vs) == 2, vs

        # 3) 主用返回空向量 → 兜底
        vk._embedding_clients = lambda: {"primary": _Client("empty"), "fallback": _Client("ok")}
        vs = vk.embed(["a", "b"])
        assert len(vs) == 2, vs

        # 4) 均失败 → 抛异常
        vk._embedding_clients = lambda: {"primary": _Client("raise"), "fallback": _Client("raise")}
        try:
            vk.embed(["a"])
            raise AssertionError("expected RuntimeError")
        except RuntimeError:
            pass

        # 5) 未配置主模型 → 直接兜底（不抛异常，返回兜底结果）
        saved_model = S.embedding_model
        S.embedding_model = ""
        vk._embedding_clients = lambda: {"primary": _Client("raise"), "fallback": _Client("ok")}
        vs = vk.embed(["a"])  # 主模型为空 → 绕过 primary 直接走 fallback
        assert vs == [[1.0, 0.0], [0.0, 1.0]], vs
        S.embedding_model = saved_model
    finally:
        vk._embedding_clients = orig_clients
        _restore(saved)


def test_llm_gateway_uses_scene_models():
    """LLMGateway 使用 AGENT_MODEL 与 AGENT_FALLBACK_MODEL（Ollama 服务器地址可配）。"""
    import app.llm_gateway as g

    saved = _patch(openrouter_api_key="sk-test",
                   openrouter_base_url="https://openrouter.ai/api/v1",
                   agent_model="openrouter/my-agent",
                   agent_fallback_model="my-ollama-model",
                   ollama_base_url="http://ollama-custom:11434/v1")
    calls = []

    def fake_openai(messages, model, **kw):
        calls.append(("primary", model, g.settings.openrouter_base_url))
        raise RuntimeError("down")

    def fake_ollama(messages, model, base_url, temperature=0):
        calls.append(("fallback", model, base_url))
        return {"choices": [{"message": {"content": "ok"}}],
                "model": model, "usage": {}}

    o1, o2 = _patch_llm_backends(fake_openai, fake_ollama)
    try:
        assert g.settings.agent_model == "openrouter/my-agent"
        assert g.settings.agent_fallback_model == "my-ollama-model"
        assert g.settings.ollama_base_url == "http://ollama-custom:11434/v1"
        gw = g.get_llm()
        res = gw.chat([{"role": "user", "content": "hi"}])
        assert res.provider == "ollama"
        assert calls == [("primary", "openrouter/my-agent", "https://openrouter.ai/api/v1"),
                         ("fallback", "my-ollama-model", "http://ollama-custom:11434/v1")]
        assert gw.name == "openrouter:openrouter/my-agent"
    finally:
        g._openai_chat, g._ollama_chat_raw = o1, o2
        _restore(saved)


def test_relevance_check_llm_related():
    """相关性校验：LLM 判定相关 → relevant=True。"""
    import app.classifier as clf

    saved = _patch(openrouter_api_key="sk-test")
    orig_fb = clf.chat_with_fallback

    def fake_fb(messages, **kw):
        return LLMResult(content='{"relevant": true, "reason": "属于IT故障排查"}',
                         model="m", provider="openrouter", usage={})

    clf.chat_with_fallback = fake_fb
    try:
        r = clf.relevance_check("VPN 连不上", "无法连接公司 VPN")
        assert r["relevant"] is True and r["reason"] == "属于IT故障排查"
        assert r["source"] == "llm"
    finally:
        clf.chat_with_fallback = orig_fb
        _restore(saved)


def test_relevance_check_llm_irrelevant():
    """相关性校验：LLM 判定不相关 → relevant=False（建单应被拒绝）。"""
    import app.classifier as clf

    saved = _patch(openrouter_api_key="sk-test")
    orig_fb = clf.chat_with_fallback

    def fake_fb(messages, **kw):
        return LLMResult(content='{"relevant": false, "reason":"与IT工单无关"}',
                         model="m", provider="openrouter", usage={})

    clf.chat_with_fallback = fake_fb
    try:
        r = clf.relevance_check("帮我写个招聘启事", "招聘销售岗位")
        assert r["relevant"] is False and r["reason"] == "与IT工单无关"
    finally:
        clf.chat_with_fallback = orig_fb
        _restore(saved)


def test_relevance_check_fallback_allows():
    """相关性校验：LLM 不可用 → 放行（relevant=True），避免误拦。"""
    import app.classifier as clf

    saved = _patch(openrouter_api_key="sk-test")
    orig_fb = clf.chat_with_fallback

    def broken(*a, **kw):
        raise RuntimeError("backends down")

    clf.chat_with_fallback = broken
    try:
        r = clf.relevance_check("随便什么内容", "无明显关键词内容")
        assert r["relevant"] is True and r["source"] == "rule"
    finally:
        clf.chat_with_fallback = orig_fb
        _restore(saved)


def test_relevance_check_rule_reject_chat():
    """相关性校验：LLM 无返回且命中明显非 IT 闲聊关键词 → 判定不相关。"""
    import app.classifier as clf

    saved = _patch(openrouter_api_key="")
    r = clf.relevance_check("午餐吃什么", "推荐一下今天中午吃什么，随便聊聊")
    assert r["relevant"] is False, r
    assert r["source"] == "rule"
    _restore(saved)


def test_rewrite_content_llm():
    """提示词重写：LLM 返回摘要 → 使用该摘要而非原文。"""
    import app.classifier as clf

    saved = _patch(openrouter_api_key="sk-test")
    orig_fb = clf.chat_with_fallback

    def fake_fb(messages, **kw):
        return LLMResult(
            content='{"summary":"为 u-1024 开通数据库只读权限","keywords":["u-1024","只读"]}',
            model="m", provider="openrouter", usage={})

    clf.chat_with_fallback = fake_fb
    try:
        r = clf.rewrite_content("开通数据库只读账号", "给 u-1024 开通 db 只读权限")
        assert r["source"] == "llm"
        assert r["summary"] == "为 u-1024 开通数据库只读权限"
        assert "u-1024" in r["keywords"]
    finally:
        clf.chat_with_fallback = orig_fb
        _restore(saved)


def test_rewrite_content_fallback_original():
    """提示词重写：LLM 不可用 → 原样返回原文摘要，流程不中断。"""
    import app.classifier as clf

    saved = _patch(openrouter_api_key="sk-test")
    orig_fb = clf.chat_with_fallback

    def broken(*a, **kw):
        raise RuntimeError("backends down")

    clf.chat_with_fallback = broken
    try:
        r = clf.rewrite_content("VPN 连不上", "排查 VPN 连接")
        assert r["source"] == "rule"
        assert "VPN 连不上" in r["summary"] and "排查 VPN 连接" in r["summary"]
    finally:
        clf.chat_with_fallback = orig_fb
        _restore(saved)


def test_newfeature_model_override_and_fallback():
    """相关性校验/提示词重写：专用模型优先，留空则回退到 classifier 模型。"""
    s2 = _env(
        CLASSIFIER_MODEL="openrouter/classifier-base",
        CLASSIFIER_FALLBACK_MODEL="classifier-ollama",
    )
    assert s2.relevance_model == "openrouter/classifier-base"
    assert s2.relevance_fallback_model == "classifier-ollama"
    assert s2.rewrite_model == "openrouter/classifier-base"

    s3 = _env(
        CLASSIFIER_MODEL="openrouter/classifier-base",
        CLASSIFIER_FALLBACK_MODEL="classifier-ollama",
        RELEVANCE_MODEL="openrouter/relevance-strong",
        RELEVANCE_FALLBACK_MODEL="relevance-ollama",
        REWRITE_MODEL="openrouter/rewrite-strong",
        REWRITE_FALLBACK_MODEL="rewrite-ollama",
    )
    assert s3.relevance_model == "openrouter/relevance-strong"
    assert s3.relevance_fallback_model == "relevance-ollama"
    assert s3.rewrite_model == "openrouter/rewrite-strong"
    assert s3.rewrite_fallback_model == "rewrite-ollama"


def _run_all():
    fns = [(name, fn) for name, fn in sorted(globals().items())
           if name.startswith("test_") and callable(fn)]
    failed = 0
    for name, fn in fns:
        try:
            fn()
            print(f"PASS  {name}")
        except Exception as e:  # noqa: BLE001
            import traceback
            traceback.print_exc()
            failed += 1
            print(f"FAIL  {name}: {e}")
    if failed:
        print(f"\n{len(fns) - failed}/{len(fns)} passed, {failed} failed")
        sys.exit(1)
    print(f"\nALL {len(fns)} TESTS PASSED")


if __name__ == "__main__":
    _run_all()
