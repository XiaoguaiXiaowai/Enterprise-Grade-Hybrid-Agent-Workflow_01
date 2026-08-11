"""敏感数据脱敏中间件：姓名/邮箱/手机/凭证统一打码。"""
import re


def mask_principal(value: str) -> str:
    if not value:
        return value
    # 邮箱: 保留首字符+域名
    if "@" in value:
        local, domain = value.split("@", 1)
        return (local[0] + "*" * max(1, len(local) - 1)) + "@" + domain
    # 中文姓名/其他: 居中打码
    if len(value) <= 1:
        return "*"
    return value[0] + "*" * (len(value) - 2) + value[-1]


def mask_secret(value: str) -> str:
    return "***"
