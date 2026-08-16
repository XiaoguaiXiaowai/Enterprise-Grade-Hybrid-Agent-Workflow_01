"""生产环境初始化脚本（P1-10 整改）：手动创建账户，替代生产禁用的演示 seed。

用法：
    python scripts/init_prod.py --username admin --password '强密码' --role admin
    # 可重复执行创建多个账户；同用户名已存在则跳过并提示。
    # 也支持 --seed-demo：创建 employee/operator/admin 三角色演示账户（密码随机打印，
    # 仅建议在内部演示环境使用；生产请用真实用户体系）。

生产模式（APP_ENV=production）下演示 seed 被禁用，部署后必须用本脚本初始化账户，
否则系统没有任何可登录用户。
"""
import argparse
import os
import secrets
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.config import settings  # noqa: E402
from app.db import init_db, session  # noqa: E402
from app.security import hash_password  # noqa: E402

ROLES = ("employee", "operator", "admin")


def _create(username: str, password: str, role: str) -> str:
    if role not in ROLES:
        raise SystemExit(f"非法角色 {role}（可选 {ROLES}）")
    with session() as conn:
        row = conn.execute("SELECT id FROM users WHERE username=?", (username,)).fetchone()
        if row:
            return f"已存在，跳过：{username}（角色 {role}）"
        conn.execute(
            "INSERT INTO users (tenant_id, username, password_hash, role) VALUES (1,?,?,?)",
            (username, hash_password(password), role))
    return f"已创建：{username}（角色 {role}）"


def main():
    ap = argparse.ArgumentParser(description="生产环境账户初始化")
    ap.add_argument("--username", help="用户名")
    ap.add_argument("--password", help="密码（不传则交互输入）")
    ap.add_argument("--role", default="admin", choices=ROLES, help="角色")
    ap.add_argument("--seed-demo", action="store_true",
                    help="创建 employee/operator/admin 三角色演示账户（密码随机生成并打印）")
    args = ap.parse_args()

    init_db()
    with session() as conn:
        conn.execute("INSERT OR IGNORE INTO tenants (id, name) VALUES (1, '默认租户')")

    print(f"APP_ENV={settings.env}，DB={settings.abs_db_path}")
    if args.seed_demo:
        for role in ROLES:
            pw = secrets.token_urlsafe(12)
            print(" -", _create(role, pw, role), f"（本次密码：{pw}，仅本次打印，请妥善保存）")
        return

    if not args.username:
        ap.error("必须提供 --username（或使用 --seed-demo）")
    password = args.password or secrets.token_urlsafe(12)
    if not args.password:
        print(f"未提供 --password，已随机生成：{password}（仅本次打印）")
    print(" -", _create(args.username, password, args.role))


if __name__ == "__main__":
    main()
