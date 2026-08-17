"""诊断 MCP stdio server 拉起失败的**真因**（ExceptionGroup 内层展开）。

用法（在本机与容器里各跑一次对照）：
    python scripts/diag_mcp_stdio.py
输出：
  - resolve 后的启动命令与存在性
  - 该解释器里 mcp 包是否可用（最常见失败点）
  - 裸跑 cmdb server 3 秒的 stderr（stdio 握手期报错会打印在这里）
  - 走 registry 真实连接的结果（healthy / error，error 已展开 ExceptionGroup 内层）
"""
import os
import subprocess
import sys
import time

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
os.environ.setdefault("APP_ENV", "development")

from app.config import BASE_DIR  # noqa: E402
from app.mcp.config import load_config  # noqa: E402
from app.mcp.servers import _resolve_runner, _walk_exceptions  # noqa: E402


def main() -> None:
    cfg = load_config().get("cmdb")
    if cfg is None:
        print("FAIL  mcp_servers.json 中未找到 cmdb server（检查 MCP_CONFIG_PATH）")
        sys.exit(1)

    # 1) 启动命令解析
    runner = _resolve_runner(cfg.command)
    print(f"[1] resolved command : {runner}")
    print(f"    exists           : {os.path.exists(runner)}")
    show = os.environ.get("CMDB_DATA")
    print(f"    env CMDB_DATA    : {show!r}")

    args = [runner] + [str(BASE_DIR / a) for a in cfg.args]
    print(f"[2] child args       : {args}")
    print(f"    cwd              : {BASE_DIR}")

    # 2) 该解释器是否有 mcp
    try:
        r = subprocess.run(
            [runner, "-c", "import mcp; print('mcp import ok')"],
            capture_output=True, text=True, cwd=str(BASE_DIR), timeout=30)
        print(f"[3] import mcp in runner: rc={r.returncode} out={r.stdout.strip()!r} "
              f"err={r.stderr.strip()[:300]!r}")
    except Exception as e:  # noqa: BLE001
        print(f"[3] import mcp in runner: FAILED run {_walk_exceptions(e)}")

    # 3) 裸跑 server 几秒，抓 stderr（stdio 握手/初始化错误会出现在这里）
    child_env = {**os.environ, **{k: str(v) for k, v in (cfg.env or {}).items()}}
    try:
        p = subprocess.Popen(args, cwd=str(BASE_DIR), env=child_env,
                             stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        time.sleep(3)
        p.terminate()
        try:
            out, err = p.communicate(timeout=5)
        except Exception:  # noqa: BLE001
            p.kill()
            out, err = p.communicate()
        print(f"[4] server bare-run: rc={p.returncode}")
        print(f"    stdout={out.decode(errors='replace')[:300]!r}")
        print(f"    stderr={err.decode(errors='replace')[:800]!r}")
    except Exception as e:  # noqa: BLE001
        print(f"[4] server bare-run: FAILED spawn {_walk_exceptions(e)}")

    # 4) 走 registry 真实连接（错误已展开 ExceptionGroup）
    from app.mcp import servers
    conn = servers.registry.get_connection("cmdb", refresh=True)
    print(f"[5] registry connect : healthy={conn.healthy}")
    print(f"    error={conn.error if conn.error else '(none)'}")
    servers.registry.disconnect_all()

    ok = conn.healthy
    print("\n结论:", "连接正常（本机对照基准）。若容器里此步失败，看上面的展开错误定位。"
          if ok else "连接失败——见上面各步输出定位真因（最常见：容器解释器未安装 mcp，见 [3]）。")
    sys.exit(0 if ok else 2)


if __name__ == "__main__":
    main()