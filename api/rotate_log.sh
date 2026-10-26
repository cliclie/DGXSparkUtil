#!/usr/bin/env bash
# api/server.log を日次で空にする (dgx-spark-logrotate.timer が JST 0 時に実行)
#
# 挙動: server.log が空でなければ truncate して 0 バイトにするだけ。
#   アーカイブ (zip) は作らない。中身は uvicorn のアクセスログ
#   (`INFO:     IP:port - "GET …" 200 OK`) だけで行にタイムスタンプが無く、
#   保存価値がないと実運用で判断した (2026-10-26。187 MB は 1.2 MB に圧縮できたが、
#   その 1.2 MB にも残す値がなかった)。
#
# 方式: truncate (リネームしない)
#   書き手は fd を開いたまま追記し続ける (dgx-spark-api.service の
#   `StandardOutput=append:server.log` / `run.sh -d` の `>> server.log`)。fd は inode に
#   紐づくため、リネームすると書込みがリネーム先へ流れ続けて server.log は以後増えない。
#   truncate は inode/fd を維持するので API を再起動せずに済む。
#
# 日付境界は JST (LOG_TZ で上書き可)。発火時刻は dgx-spark-logrotate.timer 側が
#   JST 0 時相当 (install_service.sh / install_logrotate_user.sh がそのマシンの
#   ローカル時刻へ換算して OnCalendar に焼き込む)。
set -euo pipefail

API_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
LOG="${API_DIR}/server.log"
export TZ="${LOG_TZ:-Asia/Tokyo}"

# 多重起動防止 (timer と手動実行の重複、Persistent=true の立て続け発火)。
# ロックが取れない = 別の実行が走っている ので何もしない。
exec 9>"${API_DIR}/.rotate_log.lock"
flock -n 9 || exit 0

[ -f "$LOG" ] || exit 0                        # ログなし = 何もしない
bytes="$(stat -c %s "$LOG")"
[ "$bytes" -gt 0 ] || exit 0                   # 空なら何も出力しない

: > "$LOG"                                     # inode/fd を保ったまま 0 バイトに
echo "$(date -Iseconds) truncated ${LOG##*/} (${bytes} bytes)"
