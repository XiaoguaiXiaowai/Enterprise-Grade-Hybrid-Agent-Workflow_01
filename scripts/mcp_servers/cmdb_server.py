#!/usr/bin/env python3
"""示例 MCP Server：mcp-cmdb（CMDB 配置管理库）。

MCP（Model Context Protocol）服务端，作为本项目「外部工具面」的演示载体：
- 只读查询：query_asset / list_network_devices（低风险，可直接执行）
- 高风险变更：update_asset_owner（写操作，装配侧映射为 high 风险 → HITL 审批）

数据：JSON 持久化于 data/cmdb.json（首次运行自动 seed 演示数据）。

传输：
- 默认 stdio（被 openai-agents MCPServerStdio 拉起子进程，经 stdin/stdout JSON-RPC）
- --http 模式（streamable_http）便于人工调试（mcp inspector / curl）

依赖：mcp>=2.0（mcp 2.x 使用新的 MCPServer API，FastMCP 已移除）。
"""
from __future__ import annotations

import argparse
import asyncio
import copy
import json
import os
from pathlib import Path

BASE = Path(__file__).resolve().parent.parent.parent
# 数据文件可由环境变量覆盖（冒烟测试/多环境隔离用）；默认 data/cmdb.json。
# 注意：`os.getenv(key, default)` 在 key 存在但为**空串**时会返回空串而非 default，
# 而 Path("") == "."（当前目录）→ _load 会去读目录而报 "Is a directory: '.'"。
# 因此这里用 `or` 把空串也回退到默认（容器里 ${CMDB_DATA} 未设置展开为空串的场景）。
DATA_PATH = Path((os.getenv("CMDB_DATA") or str(BASE / "data" / "cmdb.json")))

_SEED = {
    "assets": [
        {"hostname": "web-01", "ip": "10.0.1.11", "dc": "华东-杭州-机房A",
         "rack": "A-01", "owner": "张伟强", "biz_line": "官网"},
        {"hostname": "web-02", "ip": "10.0.1.12", "dc": "华东-杭州-机房A",
         "rack": "A-01", "owner": "张伟强", "biz_line": "官网"},
        {"hostname": "db-01", "ip": "10.0.2.11", "dc": "华东-杭州-机房A",
         "rack": "B-02", "owner": "李娜芳", "biz_line": "订单中心"},
        {"hostname": "db-02", "ip": "10.0.2.12", "dc": "华东-上海-机房B",
         "rack": "B-05", "owner": "王强军", "biz_line": "订单中心"},
        {"hostname": "k8s-master-01", "ip": "10.0.3.11", "dc": "华东-杭州-机房A",
         "rack": "C-01", "owner": "赵敏敏", "biz_line": "基础设施"},
    ],
    "network_devices": [
        {"name": "core-sw-01", "rack": "C-08", "model": "S12700", "ip": "10.0.0.1"},
        {"name": "core-sw-02", "rack": "C-09", "model": "S12700", "ip": "10.0.0.2"},
        {"name": "edge-fw-01", "rack": "C-10", "model": "USG6650", "ip": "10.0.0.3"},
    ],
}


def _load() -> dict:
    if DATA_PATH.exists():
        try:
            return json.loads(DATA_PATH.read_text(encoding="utf-8"))
        except Exception:
            pass
    data = copy.deepcopy(_SEED)
    _save(data)
    return data


def _save(data: dict) -> None:
    DATA_PATH.parent.mkdir(parents=True, exist_ok=True)
    DATA_PATH.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")


def _find_asset(data: dict, hostname: str):
    for a in data.get("assets", []):
        if a["hostname"] == hostname:
            return a
    return None


def build_server():
    from mcp.server.mcpserver import MCPServer

    server = MCPServer(name="cmdb", version="1.0.0")

    @server.tool(name="query_asset",
                 description="查询服务器资产信息：负责人、机房、机架、IP、业务线（只读）。")
    def query_asset(hostname: str) -> dict:
        """查询 CMDB 服务器资产（只读）。"""
        data = _load()
        asset = _find_asset(data, hostname)
        if asset is None:
            return {"ok": False, "error": f"未找到资产: {hostname}"}
        return {"ok": True, "asset": asset}

    @server.tool(name="list_network_devices",
                 description="列出网络设备，可按机架过滤（只读）。")
    def list_network_devices(rack: str | None = None) -> list[dict]:
        """列出 CMDB 网络设备（只读）。"""
        data = _load()
        devices = data.get("network_devices", [])
        if rack:
            devices = [d for d in devices if d.get("rack") == rack]
        return devices

    @server.tool(name="update_asset_owner",
                 description="变更服务器资产负责人（写操作；企业侧映射为高风险，需 HITL 审批）。")
    def update_asset_owner(hostname: str, new_owner: str, reason: str) -> dict:
        """变更 CMDB 资产负责人（写操作）。"""
        if not new_owner or not reason:
            return {"ok": False, "error": "new_owner 与 reason 均必填"}
        data = _load()
        asset = _find_asset(data, hostname)
        if asset is None:
            return {"ok": False, "error": f"未找到资产: {hostname}"}
        old_owner = asset.get("owner", "")
        asset["owner"] = new_owner
        asset["update_reason"] = reason
        _save(data)
        return {"ok": True, "hostname": hostname,
                "old_owner": old_owner, "new_owner": new_owner,
                "note": "CMDB 负责人已更新"}

    return server


async def _amain(http: str, port: int) -> None:
    server = build_server()
    if http:
        await server.run_streamable_http_async(host=http, port=port)
    else:
        await server.run_stdio_async()


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--http", type=str, default="",
                        help="以 streamable_http 模式运行（默认 stdio）")
    parser.add_argument("--port", type=int, default=8001)
    args = parser.parse_args()
    asyncio.run(_amain(args.http, args.port))


if __name__ == "__main__":
    main()