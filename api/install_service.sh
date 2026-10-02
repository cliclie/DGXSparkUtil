#!/usr/bin/env bash
# Install the DGXSparkUtil API as a systemd system service (auto-start on boot).
# Works on any host: the repo location is derived from this script's path,
# the run user from SUDO_USER. The unit file is rendered from the __API_DIR__ /
# __USER__ placeholders in dgx-spark-api.service.
# Usage: sudo bash <repo>/api/install_service.sh
set -euo pipefail

API_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
RUN_USER="${SUDO_USER:-cliclie}"
SERVICE="dgx-spark-api.service"
UNIT_PATH="/etc/systemd/system/${SERVICE}"

if [ "$(id -u)" -ne 0 ]; then
  echo "Error: please run with sudo: sudo bash $0" >&2
  exit 1
fi

echo "==> API_DIR=${API_DIR} RUN_USER=${RUN_USER}"

echo "==> Stopping any manually started instance (frees port 8080)"
systemctl stop "${SERVICE}" 2>/dev/null || true
pkill -u "${RUN_USER}" -f 'uvicorn main:app' 2>/dev/null || true
sleep 2

echo "==> Rendering unit file -> ${UNIT_PATH}"
sed -e "s|__API_DIR__|${API_DIR}|g" -e "s|__USER__|${RUN_USER}|g" \
  "${API_DIR}/${SERVICE}" > "${UNIT_PATH}"
chmod 644 "${UNIT_PATH}"
systemctl daemon-reload

echo "==> Enabling and starting service"
systemctl enable --now "${SERVICE}"

sleep 3
echo "==> Status"
systemctl --no-pager status "${SERVICE}" | head -n 15
HTTP_CODE="$(curl -s -o /dev/null -w '%{http_code}' http://localhost:8080/)"
echo "==> HTTP check: ${HTTP_CODE}"
