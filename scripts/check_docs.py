#!/usr/bin/env python3
"""文档静态校验脚本（docs 整合优化 · 阶段3产物）。

校验范围：仓库根 README.md 与 docs/**/*.md。
校验项：
  1. 相对链接   —— markdown 链接目标（非 http(s)/mailto/#）在仓库内存在；
  2. 代码路径   —— 反引号中的仓库相对路径（app/xxx.py、scripts/xxx.py、web/...）存在；
  3. 符号存在   —— `app/state_machine.py:transition` / `app/state_machine.py:StateMachine`
                   等「文件:符号」引用中，符号（函数/类）真实存在于该文件；
  4. 配置键     —— 反引号中的全大写蛇形键（XXX_YYY）必须存在于 .env.example 或
                   app/config.py 的 os.getenv 键集合（排除协议/格式白名单）；
  5. 端口号     —— URL/localhost/127.0.0.1 中引用的端口必须在已知端口集合内
                   （docker-compose*.yml、nginx.conf、web/next.config.mjs 提取）；
  6. 映射表     —— docs/01-核心功能/06-能力-代码映射表.md 的「代码模块」列路径存在、
                   「关键函数/类」列符号存在。

用法：
  python scripts/check_docs.py [--report tmp/doc_check_report.json] [--verbose]
退出码：0 = 通过；1 = 存在错误（缺失/失效引用）；2 = 运行错误。

忽略规则：
  - 运行期/生成目录前缀（.venv/ node_modules/ .next/ logs/ data/ tmp/）不校验存在性；
  - `#` 锚点与行内代码中明显非路径的 token 不校验。
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

# 运行期/生成目录：存在性校验跳过
SKIP_EXIST_PREFIX = (".venv/", "node_modules/", ".next/", "logs/", "data/", "tmp/")

# 非配置键的协议/格式 token（反引号中的全大写词，但不是环境变量）
NON_ENV_TOKENS = {
    "HTTP", "HTTPS", "POST", "GET", "PUT", "DELETE", "PATCH", "SSE", "WS",
    "JSON", "API", "DB", "SQL", "SQLITE", "WAL", "FTS", "RRF", "MRR", "NDCG",
    "TOKEN", "IAM", "RBAC", "HITL", "LOOP", "MCP", "RAG", "LLM", "ANN", "IVF",
    "HNSW", "SQ", "PBKDF2", "TLS", "SSL", "URL", "URI", "UUID", "ID", "IP",
    "AI", "CPU", "OS", "PDF", "DOCX", "TXT", "CSV", "HTML", "CSS", "JS", "TS",
    "SPA", "SSO", "OA", "CMDB", "README", "DOCKER", "NGINX", "GITHUB", "CI",
    "YAML", "MD", "ENV", "INFRA", "STDIO", "STREAMABLE", "CHAT", "EMBED",
    "OPENAI", "OLLAMA", "OPENROUTER", "LANCE", "LANCEBED", "FASTAPI", "UVICORN",
    "NEXT", "REACT", "PYTHON", "BASH", "CURL", "QPS", "TPS", "CDN", "DNS",
    "LATENCY", "TRACE", "TRACES", "METRICS", "TICKET", "TICKETS", "AGENT",
    "AGENTS", "WORKFLOW", "WORKER", "WORKERS", "DEFAULT", "FALLBACK", "PRIMARY",
    "BACKEND", "FRONTEND", "PROD", "DEV", "TEST", "LOCAL", "REMOTE", "SECRET",
    "IVF_PQ", "IVF_HNSW_SQ", "HNSW_SQ", "COSINE", "L2", "DOT",
    "VALID_STATUSES", "TRANSITIONS", "SKIP_EXIST", "KNOWN_PORTS",
}

# 已知端口集合（从部署配置提取，运行期动态合并）
KNOWN_PORTS = {80, 443, 22, 8000, 3000, 11434, 8001}


def extract_known_ports() -> set[int]:
    """从 docker-compose*.yml / nginx.conf / web/next.config.mjs 提取端口。"""
    ports = set(KNOWN_PORTS)
    pat = re.compile(r"(?::|端口|port\s*[:=]?\s*)(\d{2,5})", re.IGNORECASE)
    files = [
        ROOT / "docker-compose.yml",
        ROOT / "docker-compose.prod.yml",
        ROOT / "nginx.conf",
        ROOT / "deploy/nginx/playground.conf",
        ROOT / "web/next.config.mjs",
    ]
    for f in files:
        if f.exists():
            try:
                text = f.read_text(encoding="utf-8", errors="ignore")
            except OSError:
                continue
            for m in pat.finditer(text):
                n = int(m.group(1))
                if 1 <= n <= 65535:
                    ports.add(n)
    return ports


def env_keys_from_code() -> set[str]:
    """从 .env.example 与 app/**/*.py 的 os.getenv 提取全部配置键。"""
    keys: set[str] = set()
    env_example = ROOT / ".env.example"
    if env_example.exists():
        for line in env_example.read_text(encoding="utf-8", errors="ignore").splitlines():
            line = line.strip().lstrip("#").strip()  # 注释掉的键也算已知（可能被引用说明）
            if line and not line.startswith("#") and "=" in line:
                keys.add(line.split("=", 1)[0].strip())
    getenv_pat = re.compile(r'os\.getenv\s*\(\s*["\']([A-Z][A-Z0-9_]*)["\']')
    for py in (ROOT / "app").rglob("*.py"):
        text = py.read_text(encoding="utf-8", errors="ignore")
        keys.update(getenv_pat.findall(text))
    return keys


def code_symbols() -> dict[str, set[str]]:
    """文件相对路径 → 该文件内的函数/类/常量名集合。

    覆盖 app/ 与 scripts/ 的 .py（函数/类/模块级常量）与 web/ 的 .ts/.tsx（export 符号）。
    """
    symbols: dict[str, set[str]] = {}
    for base in ("app", "scripts"):
        for py in (ROOT / base).rglob("*.py"):
            if "__pycache__" in py.parts:
                continue
            rel = py.relative_to(ROOT).as_posix()
            text = py.read_text(encoding="utf-8", errors="ignore")
            names = set(re.findall(r"^(?:async\s+)?def\s+(\w+)", text, re.M))
            names |= set(re.findall(r"^class\s+(\w+)", text, re.M))
            names |= set(re.findall(r"^[A-Z][A-Z0-9_]*\s*=", text, re.M))
            names = {n.rstrip(" =") for n in names}
            symbols[rel] = names
    for ts in (ROOT / "web").rglob("*.ts*"):
        if "node_modules" in ts.parts or ".next" in ts.parts:
            continue
        rel = ts.relative_to(ROOT).as_posix()
        text = ts.read_text(encoding="utf-8", errors="ignore")
        names = set(re.findall(r"^export\s+(?:async\s+)?function\s+(\w+)", text, re.M))
        names |= set(re.findall(r"^export\s+const\s+(\w+)", text, re.M))
        names |= set(re.findall(r"^(?:async\s+)?function\s+(\w+)", text, re.M))
        names |= set(re.findall(r"^const\s+(\w+)\s*=\s*(?:async\s*)?\(?[^=]*=>", text, re.M))
        symbols[rel] = names
    return symbols


BACKTICK_PAT = re.compile(r"`([^`]+)`")
LINK_PAT = re.compile(r"\[[^\]]*\]\(([^)]+)\)")

# 文件级忽略指令：<!-- check_docs-ignore-token: MODEL_PROVIDER, MODEL_NAME -->
IGNORE_DIRECTIVE_PAT = re.compile(
    r"<!--\s*check_docs-ignore-token\s*:\s*([^>]+?)\s*-->")
PATH_PAT = re.compile(r"([A-Za-z0-9_./-]+\.(?:py|tsx|mjs|ts|json|js|yaml|yml|conf|txt|md|sh|css|html|toml))")
SYMBOL_REF_PAT = re.compile(r"([A-Za-z0-9_./-]+\.(?:py|tsx|mjs|ts|js)):([A-Za-z_]\w*(?:\.[A-Za-z_]\w*)*)")
ENVKEY_PAT = re.compile(r"\b[A-Z][A-Z0-9]*(?:_[A-Z0-9]+)+\b")
PORT_REF_PAT = re.compile(r"(?:https?://[^/\s:]+|localhost|127\.0\.0\.1):(\d{2,5})")


def check_file(path: Path, env_keys: set[str], symbols: dict[str, set[str]],
               known_ports: set[int], report: list[dict], verbose: bool) -> None:
    rel = path.relative_to(ROOT).as_posix()
    text = path.read_text(encoding="utf-8", errors="ignore")
    ignore_tokens: set[str] = set()
    for dm in IGNORE_DIRECTIVE_PAT.finditer(text):
        ignore_tokens.update(t.strip() for t in dm.group(1).split(",") if t.strip())
    # 全部已知代码符号（函数/类/常量）平铺集合：命中则不当作配置键
    all_symbols: set[str] = set()
    for names in symbols.values():
        all_symbols |= names
    errors: list[dict] = []

    # 1+2+3+4：反引号内容里的路径 / 符号引用 / 配置键
    for m in BACKTICK_PAT.finditer(text):
        tok = m.group(1).strip()
        line_no = text[: m.start()].count("\n") + 1
        # 符号引用 app/xx.py:func
        for sm in SYMBOL_REF_PAT.finditer(tok):
            fpath, sym = sm.group(1), sm.group(2)
            if fpath.startswith(SKIP_EXIST_PREFIX) or not (ROOT / fpath).exists():
                continue
            sname = sym.split(".", 1)[0]
            if sname not in symbols.get(fpath, set()):
                errors.append({
                    "file": rel, "line": line_no, "kind": "symbol",
                    "ref": f"{fpath}:{sym}",
                    "detail": f"符号 {sym!r} 不存在于 {fpath}（候选：{sorted(symbols.get(fpath, set()))[:8]}…）",
                })
        # 路径引用（仅校验以仓库根前缀开头的路径，代码块目录树中的裸文件名不校验）
        if tok.startswith(("app/", "scripts/", "web/", "docs/", "deploy/", ".github/")):
            for pm in PATH_PAT.finditer(tok):
                p = pm.group(1).split("#", 1)[0]
                if not p.startswith(("app/", "scripts/", "web/", "docs/", "deploy/", ".github/")):
                    continue
                if p.startswith(SKIP_EXIST_PREFIX):
                    continue
                if not (ROOT / p).exists():
                    errors.append({
                        "file": rel, "line": line_no, "kind": "path",
                        "ref": p, "detail": f"路径不存在：{p}",
                    })
        # 配置键（仅反引号内全大写蛇形词；已知代码符号/忽略指令内的不校验）
        if ENVKEY_PAT.fullmatch(tok) and tok not in NON_ENV_TOKENS \
                and tok not in ignore_tokens and tok not in env_keys \
                and tok not in all_symbols:
            errors.append({
                "file": rel, "line": line_no, "kind": "envkey",
                "ref": tok, "detail": f"配置键 {tok} 不在 .env.example / config.py os.getenv 集合",
            })

    # 链接检查（不在反引号内的 markdown 链接）
    for m in LINK_PAT.finditer(text):
        target = m.group(1).strip()
        line_no = text[: m.start()].count("\n") + 1
        if target.startswith(("http://", "https://", "mailto:", "#", "data:")):
            continue
        t = target.split("#", 1)[0]
        if not t:
            continue
        resolved = (path.parent / t).resolve()
        if not resolved.exists():
            errors.append({
                "file": rel, "line": line_no, "kind": "link",
                "ref": target, "detail": f"链接目标不存在：{target}（解析为 {resolved.relative_to(ROOT)}）",
            })

    # 端口检查
    for m in PORT_REF_PAT.finditer(text):
        port = int(m.group(1))
        line_no = text[: m.start()].count("\n") + 1
        if port not in known_ports:
            errors.append({
                "file": rel, "line": line_no, "kind": "port",
                "ref": f":{port}", "detail": f"端口 {port} 不在已知端口集合 {sorted(known_ports)}",
            })

    report.extend(errors)
    if verbose:
        for e in errors:
            print(f"  [{e['kind']}] {e['file']}:{e['line']}  {e['ref']}  —  {e['detail']}")


def check_mapping_table(env_keys: set[str], symbols: dict[str, set[str]],
                        report: list[dict], verbose: bool) -> None:
    """映射表专项：校验「代码模块」列路径与「关键函数/类」列符号。"""
    candidates = sorted((ROOT / "docs").rglob("*.md")) if (ROOT / "docs").exists() else []
    table_file = next((p for p in candidates if "能力-代码映射表" in p.name), None)
    if table_file is None:
        report.append({"file": "docs", "line": 0, "kind": "mapping",
                       "ref": "06-能力-代码映射表.md", "detail": "未找到能力-代码映射表文档"})
        return
    rel = table_file.relative_to(ROOT).as_posix()
    for i, line in enumerate(table_file.read_text(encoding="utf-8").splitlines(), 1):
        if not line.startswith("|"):
            continue
        for sm in SYMBOL_REF_PAT.finditer(line):
            fpath, sym = sm.group(1), sm.group(2)
            if fpath.startswith(SKIP_EXIST_PREFIX) or not (ROOT / fpath).exists():
                continue
            sname = sym.split(".", 1)[0]
            if sname not in symbols.get(fpath, set()):
                report.append({"file": rel, "line": i, "kind": "mapping-symbol",
                               "ref": f"{fpath}:{sym}", "detail": f"映射表符号 {sym} 不存在于 {fpath}"})
        for pm in PATH_PAT.finditer(line):
            p = pm.group(1).split("#", 1)[0]
            if p.startswith(("app/", "scripts/", "web/", "docs/", "deploy/")) and not (ROOT / p).exists():
                report.append({"file": rel, "line": i, "kind": "mapping-path",
                               "ref": p, "detail": f"映射表路径不存在：{p}"})
    if verbose:
        for e in report:
            if e["kind"].startswith("mapping"):
                print(f"  [{e['kind']}] {e['file']}:{e['line']}  {e['ref']}  —  {e['detail']}")


def main() -> int:
    ap = argparse.ArgumentParser(description="文档静态校验：链接 / 路径 / 符号 / 配置键 / 端口 / 映射表")
    ap.add_argument("--report", default="", help="JSON 报告输出路径")
    ap.add_argument("--verbose", action="store_true", help="逐条打印错误")
    args = ap.parse_args()

    env_keys = env_keys_from_code()
    symbols = code_symbols()
    known_ports = extract_known_ports()

    targets: list[Path] = []
    readme = ROOT / "README.md"
    if readme.exists():
        targets.append(readme)
    docs_dir = ROOT / "docs"
    if docs_dir.exists():
        targets.extend(sorted(docs_dir.rglob("*.md")))
    if not targets:
        print("错误：未找到 README.md 或 docs/**/*.md", file=sys.stderr)
        return 2

    report: list[dict] = []
    for t in targets:
        check_file(t, env_keys, symbols, known_ports, report, args.verbose)
    check_mapping_table(env_keys, symbols, report, args.verbose)

    if args.report:
        Path(args.report).parent.mkdir(parents=True, exist_ok=True)
        Path(args.report).write_text(json.dumps({
            "checked_files": [str(t.relative_to(ROOT)) for t in targets],
            "errors": report,
        }, ensure_ascii=False, indent=2), encoding="utf-8")

    kinds: dict[str, int] = {}
    for e in report:
        kinds[e["kind"]] = kinds.get(e["kind"], 0) + 1
    print(f"校验完成：{len(targets)} 个文件，{len(report)} 个错误 {kinds or ''}")
    if report:
        for e in report:
            print(f"  [{e['kind']}] {e['file']}:{e['line']}  {e['ref']}  —  {e['detail']}")
        return 1
    print("全部通过 ✅")
    return 0


if __name__ == "__main__":
    sys.exit(main())
