"""RAG 检索评测基线（整改⑤）：真实知识库 + 人工标注查询集，量化 recall@1/3、MRR、nDCG@3。

用法（工作目录 = 项目根）：
    .venv/bin/python scripts/eval_rag.py                        # 当前配置跑一轮
    .venv/bin/python scripts/eval_rag.py --rerank off           # 关 rerank 对比
    .venv/bin/python scripts/eval_rag.py --compare              # rerank on/off 两轮对比表
    .venv/bin/python scripts/eval_rag.py --detail               # 打印每条命中明细

评测说明：
- 查询集基于 scripts/seed_demo.py 播种的真实文档（VPN 连接不上排查 / 数据库只读权限开通流程 /
  账号密码重置指引 / 生产服务 502 排查 / 用户目录与权限查询），2026-08-18 随播种数据更新对齐；
- 命中判定：期望标题与召回标题做子串互含（去空格/破折号/大小写）匹配；
- 指标：recall@1 / recall@3（前 1/3 条是否命中）、MRR、nDCG@3（排序质量）。
"""
from __future__ import annotations

import argparse
import math
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import app.config as cfg_mod  # noqa: E402
from app import kb  # noqa: E402

# ===== 评测集：query -> 期望命中的文档标题（子串匹配）=====
# 与 scripts/seed_demo.py 的 KB_DOCS（5 篇）对齐；口语化问法为主，附术语/编号型查询。
EVAL_SET = [
    # VPN 连接不上排查（network）
    {"q": "办公室连不上公司 VPN 怎么办", "expect": ["VPN 连接不上排查"]},
    {"q": "VPN 客户端一直无法登录", "expect": ["VPN 连接不上排查"]},
    {"q": "公司内网资源访问不了", "expect": ["VPN 连接不上排查"]},
    {"q": "怎么开通网络策略", "expect": ["VPN 连接不上排查"]},
    # 数据库只读权限开通流程（db）
    {"q": "怎么申请数据库只读账号", "expect": ["数据库只读权限开通流程"]},
    {"q": "数据库只读权限多久回收", "expect": ["数据库只读权限开通流程"]},
    {"q": "只读账号能不能写数据", "expect": ["数据库只读权限开通流程"]},
    {"q": "申请数据库权限要 DBA 审批吗", "expect": ["数据库只读权限开通流程"]},
    # 账号密码重置指引（account）
    {"q": "忘记密码怎么重置", "expect": ["账号密码重置指引"]},
    {"q": "管理员重置账号密码的流程", "expect": ["账号密码重置指引"]},
    {"q": "员工能自己重置密码吗", "expect": ["账号密码重置指引"]},
    # 生产服务 502 排查（ops）
    {"q": "生产服务报 502 了", "expect": ["生产服务 502 排查"]},
    {"q": "上线后出故障先做什么", "expect": ["生产服务 502 排查"]},
    {"q": "网关健康检查异常怎么办", "expect": ["生产服务 502 排查"]},
    # 用户目录与权限查询（iam）
    {"q": "怎么查同事的联系方式", "expect": ["用户目录与权限查询"]},
    {"q": "查询用户信息需要审批吗", "expect": ["用户目录与权限查询"]},
]


def _norm(t: str) -> str:
    return "".join(ch for ch in (t or "") if ch not in " \t\r\n-_/").lower()


def _hit(title: str, expects: list[str]) -> bool:
    nt = _norm(title)
    return bool(expects) and any(_norm(e) in nt or nt in _norm(e) for e in expects)


def _ndcg(rels: list[float], k: int = 3) -> float:
    dcg = sum(r / math.log2(i + 2) for i, r in enumerate(rels[:k]))
    idcg = sum(r / math.log2(i + 2) for i, r in enumerate(sorted(rels, reverse=True)[:k]))
    return dcg / idcg if idcg else 0.0


def eval_once(label: str = "cfg") -> dict:
    """在当前 settings 下跑一轮评测，返回指标与明细。"""
    n = len(EVAL_SET)
    r1 = r3 = mrr = ndcg = 0.0
    rows = []
    for item in EVAL_SET:
        results = kb.search(item["q"], top_k=3)
        titles = [r.get("title", "") for r in results]
        rel = [1.0 if _hit(t, item["expect"]) else 0.0 for t in titles]
        r1 += 1.0 if rel and rel[0] else 0.0
        r3 += 1.0 if any(rel) else 0.0
        for i, v in enumerate(rel, start=1):
            if v:
                mrr += 1.0 / i
                break
        ndcg += _ndcg(rel, 3)
        rows.append({"q": item["q"], "titles": titles, "rel": rel})
    return {
        "label": label, "n": n,
        "recall@1": r1 / n, "recall@3": r3 / n,
        "mrr": mrr / n, "ndcg@3": ndcg / n,
        "rows": rows,
    }


def apply_overrides(rerank=None, rewrite=None, adaptive=None, parent=None,
                    multi=None, window=None) -> None:
    """把开关覆盖到 settings 单例（各模块持有同一引用，可视化生效）。"""
    def _b(v):
        return v in ("on", "true", "1", True)
    if rerank is not None:
        cfg_mod.settings.rerank_enabled = _b(rerank)
    if rewrite is not None:
        cfg_mod.settings.query_rewrite_enabled = _b(rewrite)
    if adaptive is not None:
        cfg_mod.settings.adaptive_rerank_weight = _b(adaptive)
    if parent is not None:
        cfg_mod.settings.return_parent_chunk = _b(parent)
    if multi is not None:
        cfg_mod.settings.rag_multi_query_enabled = _b(multi)
    if window is not None:
        cfg_mod.settings.rag_context_window = int(window)


def _print_table(results: list[dict]) -> None:
    print(f"\n{'label':<16}{'recall@1':>9}{'recall@3':>9}{'mrr':>8}{'ndcg@3':>8}  n")
    for r in results:
        print(f"{r['label']:<16}{r['recall@1']:>9.3f}{r['recall@3']:>9.3f}"
              f"{r['mrr']:>8.3f}{r['ndcg@3']:>8.3f}  {r['n']}")
    if len(results) == 2:
        a, b = results
        print("-" * 56)
        print(f"{'diff':<16}{b['recall@1']-a['recall@1']:>+9.3f}"
              f"{b['recall@3']-a['recall@3']:>+9.3f}"
              f"{b['mrr']-a['mrr']:>+8.3f}{b['ndcg@3']-a['ndcg@3']:>+8.3f}")


def _print_detail(r: dict) -> None:
    for row in r["rows"]:
        print(f"\nQ: {row['q']}")
        if not row["titles"]:
            print("   (无召回)")
            continue
        for t, v in zip(row["titles"], row["rel"]):
            print(f"   {'HIT ' if v else 'miss'} {t[:34]!r}")


def main() -> None:
    p = argparse.ArgumentParser(description="RAG 检索评测基线")
    p.add_argument("--rerank", choices=["on", "off"], help="rerank 开关")
    p.add_argument("--rewrite", choices=["on", "off"], help="query 改写开关")
    p.add_argument("--adaptive", choices=["on", "off"], help="自适应加权开关")
    p.add_argument("--parent", choices=["on", "off"], help="返回父块上下文开关")
    p.add_argument("--multi", choices=["on", "off"], help="多路召回开关")
    p.add_argument("--window", type=int, help="上下文窗口大小（0=关）")
    p.add_argument("--compare", action="store_true", help="对比 rerank on/off 两轮")
    p.add_argument("--detail", action="store_true", help="打印命中明细")
    args = p.parse_args()

    if args.compare:
        apply_overrides(rerank="off", rewrite=args.rewrite, adaptive=args.adaptive,
                        multi=args.multi, window=args.window)
        base = eval_once("rerank=off")
        apply_overrides(rerank="on", rewrite=args.rewrite, adaptive=args.adaptive,
                        multi=args.multi, window=args.window)
        on = eval_once("rerank=on")
        print("\n=== RAG 评测：rerank 开关对比 ===")
        _print_table([base, on])
        return

    if args.multi:
        apply_overrides(rewrite=args.rewrite, adaptive=args.adaptive, window=args.window,
                        multi="off")
        base = eval_once("multi=off")
        apply_overrides(rewrite=args.rewrite, adaptive=args.adaptive, window=args.window,
                        multi="on")
        on = eval_once("multi=on")
        print("\n=== RAG 评测：多路召回开关对比 ===")
        _print_table([base, on])
        return

    apply_overrides(rerank=args.rerank, rewrite=args.rewrite,
                    adaptive=args.adaptive, parent=args.parent,
                    multi=args.multi, window=args.window)
    res = eval_once()
    print("\n=== RAG 检索评测 ===")
    _print_table([res])
    if args.detail:
        _print_detail(res)


if __name__ == "__main__":
    main()