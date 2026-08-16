#!/usr/bin/env bash
# 服务器端部署脚本：由 GitHub Actions（.github/workflows/deploy.yml）在 push main 后经 SSH 触发。
# 运行环境：阿里云服务器 /opt/git/enterprise-agent-workflow（仓库克隆 + 本地 .env）
# 前置条件：
#   - .env 已配置（OPENROUTER_API_KEY 等真实密钥，gitignore 不提交）
set -euo pipefail
cd "$(dirname "$0")/.."

COMPOSE="docker compose -f docker-compose.prod.yml"

echo "==> [1/6] 校验 .env"
[ -f .env ] || { echo "错误: 缺少 .env（参考 .env.example 配置）"; exit 1; }

echo "==> [2/6] 同步主机 nginx 站点配置（HTTPS 入口）"
NGINX_SITE=/etc/nginx/sites-available/aep-playground.conf
cp deploy/nginx/playground.conf "$NGINX_SITE"
nginx -t
systemctl reload nginx
echo "    nginx 已重载"

echo "==> [3/6] 构建并启动容器（app: 127.0.0.1:8001 | web: 127.0.0.1:3000）"
$COMPOSE up -d --build

echo "==> [4/6] 清理悬空镜像"
docker image prune -f >/dev/null || true

echo "==> [5/6] 等待后端健康检查"
ok=0
for i in $(seq 1 60); do
  if curl -fsS http://127.0.0.1:8001/health >/dev/null 2>&1; then
    ok=1
    echo "    后端健康（第 ${i} 次探测）"
    break
  fi
  sleep 2
done
if [ "$ok" != "1" ]; then
  echo "错误: 后端健康检查失败"
  $COMPOSE ps
  exit 1
fi

echo "==> [6/6] 公网验证 + 服务状态"
curl -fsS --max-time 10 https://playground.pangliantagege.top/health && echo " <- https OK"
$COMPOSE ps
echo "==> 部署完成"
