"""MCP（Model Context Protocol）集成：外部工具/数据源的标准接入层。

核心约定（见 doc/MCP集成设计方案.md）：
- MCP server 的工具**不会直接挂给模型**，必须过映射表白名单（app/mcp/config.py）
  并享受与本地工具完全一致的治理链路（预算 / 去重 / tool_calls 落库 / trace /
  脱敏 / HITL 审批），由 app/mcp/bridge.py 实现。
- 连接生命周期（懒连接 / 健康检查 / 专用事件循环）由 app/mcp/servers.py 管理。
- 未启用 MCP（settings.mcp_enabled=False）或 SDK 不可用时，全链路优雅降级为空。
"""
from . import config as config_module
from . import servers
from . import bridge

__all__ = ["config_module", "servers", "bridge"]