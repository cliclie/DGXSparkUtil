#!/usr/bin/env bash
# Install the DGXSparkUtil API as a systemd system service (auto-start on boot).
# Usage: sudo bash /home/cliclie/DGXSparkUtil/api/install_service.sh
set -euo pipefail

API_DIR="/home/cliclie/DGXSparkUtil/api"
SERVICE="dgx-spark-api.service"
UNIT_PATH="/etc/systemd/system/${SERVICE}"

if [ "$(id -u)" -ne 0 ]; then
  echo "Error: please run with sudo: sudo bash $0" >&2
  exit 1
fi

echo "==> Stopping any manually started instance (frees port 8080)"
systemctl stop "${SERVICE}" 2>/dev/null || true
pkill -u cliclie -f 'uvicorn main:app' 2>/dev/null || true
sleep 2

echo "==> Installing unit file -> ${UNIT_PATH}"
install -m 644 "${API_DIR}/dgx-spark-api.service" "${UNIT_PATH}"
systemctl daemon-reload

echo "==> Enabling and starting service"
systemctl enable --now "${SERVICE}"

sleep 3
echo "==> Status"
systemctl --no-pager status "${SERVICE}" | head -n 15
HTTP_CODE="$(curl -s -o /dev/null -w '%{http_code}' http://localhost:8080/)"
echo "==> HTTP check: ${HTTP_CODE}"
