"""只读工具：企业知识库检索（RAG 占位，M1 用关键词匹配）。低风险。"""
from .registry import register_tool

_KB = {
    "vpn": "公司 VPN 连接不上：请先确认已安装企业根证书，再检查是否使用公司内网账号登录。",
    "password": "账号密码初始化：统一走自服务重置入口，管理员重置需提交高危工单。",
    "db": "数据库只读权限开通：需填写申请用途，由 DBA 审批后发放最小权限账号。",
    "default": "未命中知识库，建议咨询对应负责人或补充工单描述。",
}


@register_tool(name="search_kb", risk="low", description="检索企业知识库，返回匹配的运维/IT 文档片段（只读）。")
def search_kb(query: str, top_k: int = 3) -> dict:
    lowered = query.lower()
    hits = []
    for key, text in _KB.items():
        if key in lowered:
            hits.append({"topic": key, "text": text})
    if not hits:
        hits.append({"topic": "no_match", "text": _KB["default"]})
    return {"hits": hits[:top_k], "source": "local_kb"}
