"""RAG 新增三能力验证（整改⑧-续）：上下文窗口 / 多路召回 / 模糊追问——独立可跑断言。

用法（工作目录 = 项目根）：
    .venv/bin/python scripts/verify_rag_features.py

断言内容：
1. ④ 上下文窗口：vector_kb.search 返回 context（命中块+前后邻居拼接），
   RAG_CONTEXT_WINDOW=0 时无 context；
2. ② 多路召回：RAG_MULTI_QUERY_ENABLED=true 时 build_query_variants 生成 ≥2 变体，
   kb.search 结果非空且按 title 去重；
3. ① 模糊追问：无命中/低置信查询 → search_kb 返回 needs_clarification + clarify_questions，
   RAG_CLARIFY_ENABLED=false 时不触发。
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import app.config as cfg_mod  # noqa: E402
from app import kb, vector_kb, rerank  # noqa: E402
from app.skills import kb as skill_kb  # noqa: E402

_PASS = 0
_FAIL = 0


def check(name: str, cond: bool, detail: str = "") -> None:
    global _PASS, _FAIL
    if cond:
        _PASS += 1
        print(f"PASS  {name}")
    else:
        _FAIL += 1
        print(f"FAIL  {name}  {detail}")


def main() -> None:
    # ---- ① 上下文窗口 ----
    orig_w = cfg_mod.settings.rag_context_window
    cfg_mod.settings.rag_context_window = 1
    hits = vector_kb.search("离职员工权限回收流程", top_k=3)
    has_ctx = any(h.get("context") for h in hits)
    check("窗口=1: 向量命中带 context", has_ctx,
          f"hits={[(h.get('title','')[:8], bool(h.get('context'))) for h in hits]}")
    if hits and any(h.get("content") for h in hits):
        h = next((x for x in hits if x.get("context")), hits[0])
        # 单块文档（如短演示文档）无邻居：context==content；多块文档：context 应更长
        ok_wider = len(h.get("context", "")) >= len(h.get("content", ""))
        check("context 覆盖完整内容 ≥ content（单块相等/多块更长）", ok_wider,
              f"ctx={len(h.get('context',''))} content={len(h.get('content',''))}")
    cfg_mod.settings.rag_context_window = 0
    hits0 = vector_kb.search("离职员工权限回收流程", top_k=3)
    check("窗口=0: 无 context 字段", all(h.get("context") in (None, "") for h in hits0))
    cfg_mod.settings.rag_context_window = orig_w

    # ---- ② 多路召回 ----
    orig_multi = cfg_mod.settings.rag_multi_query_enabled
    cfg_mod.settings.rag_multi_query_enabled = True
    orig_rewrite = cfg_mod.settings.query_rewrite_enabled
    cfg_mod.settings.query_rewrite_enabled = False  # 禁用 LLM，只看规则变体
    variants = rerank.build_query_variants("开通数据库只读账号需要什么审批流程")
    check("多路: 变体 ≥2（原句+关键词/改写）", len(variants) >= 2, f"variants={variants}")
    res = kb.search("开通数据库只读账号需要什么审批流程", top_k=3)
    titles = [r.get("title") for r in res]
    check("多路: 结果非空且 title 去重",
          bool(res) and len(titles) == len(set(titles)), f"titles={titles}")
    cfg_mod.settings.rag_multi_query_enabled = False
    cfg_mod.settings.query_rewrite_enabled = orig_rewrite
    variants_single = rerank.build_query_variants("随便一个查询")
    check("单路: 仅原句", variants_single == ["随便一个查询"], f"variants={variants_single}")

    # ---- ③ 模糊追问 ----
    orig_clarify = cfg_mod.settings.rag_clarify_enabled
    cfg_mod.settings.rag_clarify_enabled = True
    out = skill_kb.search_kb("完全不存在的内容12345", top_k=3)
    check("澄清: 无命中触发信号",
          out.get("needs_clarification") is True and out.get("clarify_questions"),
          f"out keys={list(out.keys())}")
    cfg_mod.settings.rag_clarify_enabled = False
    out2 = skill_kb.search_kb("完全不存在的内容12345", top_k=3)
    check("澄清: 开关关闭不触发",
          out2.get("needs_clarification") is None and not out2.get("clarify_questions"))
    cfg_mod.settings.rag_clarify_enabled = orig_clarify

    print(f"\n结果: {_PASS} passed, {_FAIL} failed")
    sys.exit(1 if _FAIL else 0)


if __name__ == "__main__":
    main()