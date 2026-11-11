#!/usr/bin/env bash
# DGXSparkUtil (dgx-spark-api) 再起動 + 検証
# 用途: api/ 配下 (config / vllm / rag / main 等) を変更した後の反映。
#   $ bash api/restart_service.sh
#
# 注意:
# - 稼働中モデルは停止しない。atom1 / atom2 は compose 環境でモデルが docker コンテナ
#   (dockerd の cgroup) なので、dgx-spark-api の再起動には巻き込まれない。
# - front/ のみの変更は API 再起動が不要 (Cache-Control: no-cache で次回アクセスに自動反映)。
set -euo pipefail
SVC=dgx-spark-api
# PORT は実 unit の ExecStart (--port NNNN) から取る。
# install_service.sh は install_service.env の PORT (本機 WSL = 8090) で render するため、
# ここをハードコード (旧 8080) だと応答確認が常に失敗して誤 FAIL になる。
PORT="${PORT:-$(sed -n 's/.*--port[= ]\([0-9][0-9]*\).*/\1/p' "/etc/systemd/system/${SVC}.service" 2>/dev/null | tail -1)}"
if [ -z "${PORT}" ]; then
  PORT="$(grep -sE '^[[:space:]]*PORT=' "$(dirname "${BASH_SOURCE[0]}")/install_service.env" | tail -1 | cut -d= -f2 | tr -d '[:space:]')"
fi
PORT="${PORT:-8080}"
echo "==> 確認先ポート: ${PORT}"

echo "==> ${SVC} を再起動"
sudo systemctl restart "${SVC}"

# 起動待機: API が実際に応答するまで最大 30 秒。
# is-active はポートバインド前に active になるため不十分 (再起動直後の race)。
ready=0
for i in $(seq 1 30); do
  if curl -sf --max-time 2 "http://localhost:${PORT}/api/platform" > /dev/null 2>&1; then
    ready=1; break
  fi
  sleep 1
done
if [ "${ready}" -ne 1 ]; then
  echo "FAIL: 30 秒以内に API が応答しなかった"
  journalctl -u "${SVC}" -n 20 --no-pager
  exit 1
fi

echo "==> 状態確認"
systemctl is-active "${SVC}"

echo "==> API 応答確認"
curl -sf --max-time 5 "http://localhost:${PORT}/api/platform" > /dev/null && echo "  /api/platform 200"
curl -sf --max-time 5 "http://localhost:${PORT}/api/vllm/status" > /dev/null && echo "  /api/vllm/status 200"
curl -sf --max-time 5 "http://localhost:${PORT}/" > /dev/null && echo "  / (index.html) 200"

echo "==> 直近ログ (エラー確認)"
journalctl -u "${SVC}" -n 5 --no-pager

echo "OK: ${SVC} 再起動完了"