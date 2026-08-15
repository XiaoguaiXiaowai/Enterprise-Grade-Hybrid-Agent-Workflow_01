"""造演示数据：账密、知识库、几类示例工单。幂等可重复执行。"""
import os, sys
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
# 演示脚本强制非生产环境（整改⑥：APP_ENV=production 时 auth.seed_users 不再自动播种）
os.environ.setdefault("APP_ENV", "development")
os.environ.setdefault("SEED_USER_PASSWORD", "demo-pass-2026")  # 演示脚本专用；生产勿写死
os.environ.setdefault("APP_DB_PATH", "data/app.db")

from app.db import init_db, session
from app.kb import init_kb, seed_kb
from app.api.auth import seed_users
from app.security import hash_password
from app import daos

KB_DOCS = [
    {"title": "VPN 连接不上排查", "tag": "network",
     "content": "1.确认已安装企业根证书 2.检查是否使用公司内网账号 3.联系网络组开通网络策略 4.重启 VPN 客户端"},
    {"title": "数据库只读权限开通流程", "tag": "db",
     "content": "申请需填写用途，DBA 审批后发放最小权限账号；只读账号严禁写操作；默认 30 天回收。"},
    {"title": "账号密码重置指引", "tag": "account",
     "content": "员工可走自服务重置；管理员重置涉及高危工单，需双人复核。"},
    {"title": "生产服务 502 排查", "tag": "ops",
     "content": "先查网关与上游健康检查，确认最近一次发布窗口，若有回滚点优先回滚再定位。"},
    {"title": "用户目录与权限查询", "tag": "iam",
     "content": "通过用户目录服务查询；涉及个人敏感信息需脱敏展示。"},
]


def main():
    init_db()
    init_kb()
    seed_users()
    seed_kb(KB_DOCS)
    print(f"知识库已种入 {len(KB_DOCS)} 条文档")

    # 取 admin id
    with session() as conn:
        admin_id = conn.execute("SELECT id FROM users WHERE username='admin'").fetchone()["id"]

    existing = daos.list_tickets(1)
    if existing:
        print(f"已有 {len(existing)} 张工单，跳过造单")
        return

    tickets = [
        {"title": "VPN 连接不上", "description": "办公室网络下无法连接公司 VPN",
         "risk_level": "low", "intent_type": "knowledge"},
        {"title": "给 u-1024 开通数据库只读账号", "description": "为 u-1024 申请数据库只读权限",
         "risk_level": "high", "intent_type": "change"},
        {"title": "生产服务 502", "description": "生产服务 A 最近 5 分钟持续 502",
         "risk_level": "medium", "intent_type": "troubleshoot"},
    ]
    for t in tickets:
        tid = daos.create_ticket(1, admin_id, t["title"], t["description"],
                                 risk_level=t["risk_level"], intent_type=t["intent_type"])
        daos.add_event(tid, "created", {"title": t["title"]}, "seed")
        print(f" - 工单 #{tid}: {t['title']} ({t['risk_level']})")

    print(f"演示数据就绪。ERP 演示账户: admin / operator / employee"
          f"（密码 = {os.environ['SEED_USER_PASSWORD']}，可用 SEED_USER_PASSWORD 覆盖）")


if __name__ == "__main__":
    main()
