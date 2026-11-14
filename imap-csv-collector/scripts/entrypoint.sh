#!/usr/bin/env bash
# 容器入口：api 启动 FastAPI；imap 启动本地 IMAP 样例服务。
set -euo pipefail

case "${1:-api}" in
  api)
    exec uvicorn app.main:app --host 0.0.0.0 --port 8000
    ;;
  imap)
    exec python -m imap_server.cli serve \
      --host 0.0.0.0 --port 10143 \
      --root "${MAIL_ROOT:-/srv/mail}" \
      --username "${MAIL_USER:-instrument@lab.local}" \
      --password "${MAIL_PASSWORD:-labpass}"
    ;;
  *)
    exec "$@"
    ;;
esac
