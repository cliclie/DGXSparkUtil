#!/usr/bin/env bash
# api/server.log を日次で zip アーカイブする (dgx-spark-logrotate.timer が JST 0 時に実行)
#
# 挙動
#   - server.log は「当日 (JST) 分だけ」を保つ
#   - 実行時点で server.log に溜まっている内容を server.log.YYYYMMDD.zip に保存し、
#     server.log を 0 バイトにする
#   - YYYYMMDD = server.log の最終更新日時 (mtime) の JST 日付
#       * JST 0 時発火なら「たった今終わった日」= 前日になる
#       * 昼間に手動実行 (`sudo systemctl start dgx-spark-logrotate`) したときは
#         当日の日付になり、日付と中身がずれない
#       * 複数日溜まっていた (マシン停止 / timer 未発火) 場合は最終日の名前で 1 本
#
# 方式: コピー -> truncate -> zip (logrotate の copytruncate と同じ発想)
#   dgx-spark-api.service は `StandardOutput=append:server.log` で開いた fd に追記し
#   続けるため、リネーム方式は書込みがアーカイブ側へ流れ続ける (fd は inode に紐づく)。
#   run.sh -d の `>> server.log` (O_APPEND) も同じ理由で NG。truncate は inode/fd を
#   維持するので API を再起動せずにローテートできる。代償としてコピー開始~truncate 間
#   (数十 ms) の書込みは失われる (uvicorn のアクセスログのみ、実害なし)。
#
# 日付境界は JST 固定。日本に夏時間はないので +0900 固定 (LOG_TZ で上書き可)。
#   アーカイブ名は上の mtime 変換で決まり、タイマーの発火時刻は
#   dgx-spark-logrotate.timer 側が JST 0 時相当で発火する (両方 JST 基準)。
set -euo pipefail

API_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
LOG="${API_DIR}/server.log"
export TZ="${LOG_TZ:-Asia/Tokyo}"

# 多重起動防止 (timer と手動実行の重複、Persistent=true の立て続け発火)。
# ロックが取れない = 別のローテートが実行中 なので何もしない。
exec 9>"${API_DIR}/.rotate_log.lock"
flock -n 9 || exit 0

[ -f "$LOG" ] || exit 0                       # ログなし = 何もしない
bytes="$(stat -c %s "$LOG")"
[ "$bytes" -gt 0 ] || exit 0                  # 空ファイルの zip は作らない

day="$(date -d "@$(stat -c %Y "$LOG")" +%Y%m%d)"   # 最終書込みの JST 日付
zip_path="${API_DIR}/server.log.${day}.zip"
seq=2
while [ -e "$zip_path" ]; do                   # 同日分の再実行は連番で分ける
  zip_path="${API_DIR}/server.log.${day}.${seq}.zip"
  seq=$((seq + 1))
done

# zip 内のエントリ名を server.log にするため、作業ディレクトリで同名にコピーする
tmpdir="$(mktemp -d "${API_DIR}/.rotate_log.XXXXXX")"
trap 'rm -rf "$tmpdir"' EXIT
cp -- "$LOG" "${tmpdir}/server.log"
: > "$LOG"                                    # inode/fd を保ったまま 0 バイトに
( cd -- "$tmpdir" && zip -q "$zip_path" server.log )

echo "$(date -Iseconds) rotated ${bytes} bytes -> ${zip_path##*/} ($(stat -c %s "$zip_path") bytes)"
