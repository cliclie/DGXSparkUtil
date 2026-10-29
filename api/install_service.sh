#!/usr/bin/env bash
# Install the DGXSparkUtil API as a systemd system service (auto-start on boot).
# Works on any host: the repo location is derived from this script's path,
# the run user from SUDO_USER. The unit file is rendered from the __API_DIR__ /
# __USER__ / __PORT__ placeholders in dgx-spark-api.service.
# Usage: sudo bash <repo>/api/install_service.sh
#   (unit の __API_DIR__ / __USER__ / __PORT__ / __KILL_MODE__ / __LIMIT_NOFILE__ を置換)
#   既定ポート (8080) を変えたい / サーマルガードを入れない環境は、このスクリプトと
#   同じディレクトリの install_service.env (git 管理外・各マシン固有) に書く:
#       PORT=8090
#       KILL_MODE=process
#       LIMIT_NOFILE=1048576
#       DGXUTIL_THERMAL=0   # WSL2: 0/未指定 = 載せない (既定)、1 = 監視専らで載せる
#   → sudo は環境変数を引き継がない (WSL の sudo は -E 非対応) ため、ファイル側で持つ。
#   呼び出し側で明示した値はファイルより優先される:
#       sudo PORT=8091 bash <repo>/api/install_service.sh
#
# GPU サーマルガード (thermal-guard.service) は nvidia-smi -lgc / power1_cap を
# 直接叩く root unit。WSL2 は GPU がホスト (Windows) 側ドライバ管理でこれらの
# コマンドが効かないため、既定 (DGXUTIL_THERMAL 未指定 / =0) はスキップする
# (atom1 / atom2 は従来どおり有効化)。
# WSL2 で他機と状態を揃えたい運用向けに、DGXUTIL_THERMAL=1 なら「監視専ら」で導入する:
# unit に Environment=THERMAL_CONTAINERS= (空) を注入して clock モードの発動対象
# (docker コンテナ名) を構造的に無くすので、nvidia-smi -lgc は叩かない。読み取り
# (nvidia-smi --query / sysfs / docker ps) と /run/thermal/state の更新だけが行われ、
# API の /api/thermal が enabled:true を返す (= フロントのサーマル表示が他機と揃う)。
set -euo pipefail

API_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
RUN_USER="${SUDO_USER:-cliclie}"
SERVICE="dgx-spark-api.service"
UNIT_PATH="/etc/systemd/system/${SERVICE}"

# ホスト固有の設定 (存在しなければ何もしない = atom1 / atom2 は従来どおり)。
# 呼び出し側が明示した変数 (例: PORT=8091 bash install_service.sh) は優先する。
INSTALL_ENV="${API_DIR}/install_service.env"
if [ -f "${INSTALL_ENV}" ]; then
  while IFS='=' read -r _k _v; do
    case "${_k}" in ''|'#'*) continue ;; esac
    case "${_k}" in *[!A-Za-z0-9_]*) continue ;; esac
    if [ -z "${!_k+x}" ]; then
      export "${_k}=${_v}"
    fi
  done < "${INSTALL_ENV}"
fi

PORT="${PORT:-8080}"
# unit の KillMode。既定 control-group (従来動作)。modelctl (WSL) のように
# モデルサーバーをこの unit の cgroup 内で起動する環境は install_service.env で
# KILL_MODE=process を指定し、API 再起動からモデルを守る。
KILL_MODE="${KILL_MODE:-control-group}"
# unit の LimitNOFILE。空なら行を削除して systemd 既定 (1024) のままにする。
# WSL2 は CUDA のハンドル数が既定値を超えるため install_service.env で
# LIMIT_NOFILE=1048576 を指定する。
LIMIT_NOFILE="${LIMIT_NOFILE:-}"

# WSL2 判定 (/proc/version に microsoft)。atom1 / atom2 では常に偽 = 従来動作。
IS_WSL=0
if grep -qiE 'microsoft|wsl' /proc/version 2>/dev/null; then
  IS_WSL=1
fi
TG_ENABLED="${DGXUTIL_THERMAL:-1}"
# WSL2 は監視専らのオプトイン: DGXUTIL_THERMAL=1 のときだけ導入する。
# TG_MONITOR_ONLY=1 なら unit に Environment=THERMAL_CONTAINERS= (空) を注入し、
# クロック書き換え (nvidia-smi -lgc) が起こらないようにする
TG_MONITOR_ONLY=0
if [ "${IS_WSL}" = "1" ]; then
  if [ "${DGXUTIL_THERMAL:-}" = "1" ]; then
    TG_ENABLED=1
    TG_MONITOR_ONLY=1
  else
    TG_ENABLED=0
  fi
fi

if [ "$(id -u)" -ne 0 ]; then
  echo "Error: please run with sudo: sudo bash $0" >&2
  exit 1
fi

TG_TAG=""
if [ "${TG_MONITOR_ONLY}" = "1" ]; then TG_TAG=" (monitor-only: THERMAL_CONTAINERS=)"; fi
echo "==> API_DIR=${API_DIR} RUN_USER=${RUN_USER} PORT=${PORT} KILL_MODE=${KILL_MODE} LIMIT_NOFILE=${LIMIT_NOFILE:-既定(1024)} thermal-guard=${TG_ENABLED}${TG_TAG}"

echo "==> Stopping any manually started instance (frees port ${PORT})"
systemctl stop "${SERVICE}" 2>/dev/null || true
pkill -u "${RUN_USER}" -f 'uvicorn mai[n]:app' 2>/dev/null || true
sleep 2

echo "==> Rendering unit file -> ${UNIT_PATH}"
sed -e "s|__API_DIR__|${API_DIR}|g" -e "s|__USER__|${RUN_USER}|g" \
  -e "s|__PORT__|${PORT}|g" -e "s|__KILL_MODE__|${KILL_MODE}|g" \
  -e "s|__LIMIT_NOFILE__|${LIMIT_NOFILE}|g" \
  "${API_DIR}/${SERVICE}" > "${UNIT_PATH}"
# 未指定の行は落として従来動作 (systemd 既定 = LimitNOFILE 1024) に戻す
sed -i -e '/^LimitNOFILE=$/d' "${UNIT_PATH}"
chmod 644 "${UNIT_PATH}"

# GB10 サーマルガード。nvidia-smi -lgc / -rgc を叩くのはこの root unit だけ。
# dgx-spark-api.service は cliclie 権限のまま (User=__USER__) 据え置く。
if [ "${TG_ENABLED}" = "1" ]; then
  TG_SERVICE="thermal-guard.service"
  TG_UNIT_PATH="/etc/systemd/system/${TG_SERVICE}"
  chmod 755 "${API_DIR}/thermal_guard.sh"
  TG_SED=(-e "s|__API_DIR__|${API_DIR}|g")
  if [ "${TG_MONITOR_ONLY}" = "1" ]; then
    # 発動対象 (docker コンテナ名) を空にして「読み取り + 状態ファイル更新」だけさせる。
    # Environment= は [Service] セクション内でなければならないので ExecStart の直後に挿入する
    TG_SED+=(-e "s|^ExecStart=.*|&\nEnvironment=THERMAL_CONTAINERS=\nEnvironment=THERMAL_MODE=clock|")
  fi
  sed "${TG_SED[@]}" "${API_DIR}/${TG_SERVICE}" > "${TG_UNIT_PATH}"
  chmod 644 "${TG_UNIT_PATH}"
fi
# server.log の日次 truncate (rotate_log.sh + JST 0 時タイマー)。アーカイブは作らない。
# OnCalendar はマシンのローカル TZ で評価され、unit の TZ 環境変数は ExecStart に
# しか効かない。そこで「JST 0 時 = このマシンの何時何分か」をここで換算する。
# 日本に夏時間はないので +0900 固定 = 通年同じ時刻 (JST 機は 00:00:00、UTC 機は 15:00:00)。
LR_SERVICE="dgx-spark-logrotate.service"
LR_TIMER="dgx-spark-logrotate.timer"
JST_MIDNIGHT_EPOCH="$(TZ=Asia/Tokyo date -d 'today 00:00:00' +%s)"
JST_MIDNIGHT_LOCAL="*-*-* $(date -d "@${JST_MIDNIGHT_EPOCH}" +%H:%M:%S)"
chmod 755 "${API_DIR}/rotate_log.sh"
echo "==> Rendering log-rotate units -> ${LR_SERVICE} / ${LR_TIMER} (OnCalendar='${JST_MIDNIGHT_LOCAL}' = JST 0 時)"
sed -e "s|__API_DIR__|${API_DIR}|g" -e "s|__USER__|${RUN_USER}|g" \
  "${API_DIR}/${LR_SERVICE}" > "/etc/systemd/system/${LR_SERVICE}"
sed -e "s|__API_DIR__|${API_DIR}|g" -e "s|__USER__|${RUN_USER}|g" \
  -e "s|__JST_MIDNIGHT_LOCAL__|${JST_MIDNIGHT_LOCAL}|g" \
  "${API_DIR}/${LR_TIMER}" > "/etc/systemd/system/${LR_TIMER}"
chmod 644 "/etc/systemd/system/${LR_SERVICE}" "/etc/systemd/system/${LR_TIMER}"
systemctl daemon-reload

echo "==> Enabling and starting services"
systemctl enable --now "${SERVICE}"
if [ "${TG_ENABLED}" = "1" ]; then
  # enable --now は「稼働中の unit を再起動しない」。thermal-guard は unit を更新する
  # (Environment のしきい値変更) ので、再実行時に必ず restart しないと旧環境変数のまま
  # 動き続ける (2026-10-16 に実際に踏んだ: low_c=80 が反映されなかった)。
  systemctl enable "${TG_SERVICE}" >/dev/null 2>&1
  systemctl restart "${TG_SERVICE}"
else
  echo "==> Skipping thermal-guard (DGXUTIL_THERMAL=0 / 未指定。WSL2 は =1 のとき監視専らで入る)"
fi

# server.log 日次 truncate。タイマーを有効化し、導入時に一度 truncate して溜まった
# server.log を空にする。API 本体は停止しない (開いた fd を保つ truncate のみ)。
echo "==> Enabling ${LR_TIMER} and running the first truncate"
systemctl enable --now "${LR_TIMER}"
systemctl start "${LR_SERVICE}"

# R9700 (atom2): 旧 llm-gpu-powercap.service は power1_cap を 210W に常時固定する
# oneshot。thermal-guard が power1_cap の唯一の書き手になるので無効化する
# (残すと再起動時に 210W へ戻され、80℃ 以下で 300W にならない)。
# 判定は list-unit-files の grep ではなく unit ファイルの存在で見る
# (set -o pipefail 下では grep -q の早期終了が SIGPIPE になり条件が常に偽になる)。
if [ ! -e /proc/driver/nvidia/version ] \
   && ls /sys/class/drm/card*/device/hwmon/hwmon*/power1_cap >/dev/null 2>&1 \
   && [ -e /etc/systemd/system/llm-gpu-powercap.service ]; then
  echo "==> Disabling legacy llm-gpu-powercap.service (power1_cap is owned by thermal-guard)"
  systemctl disable --now llm-gpu-powercap.service >/dev/null 2>&1 || true
fi

sleep 3
echo "==> Status"
systemctl --no-pager status "${SERVICE}" | head -n 15
HTTP_CODE="$(curl -s -o /dev/null -w '%{http_code}' "http://localhost:${PORT}/")"
echo "==> HTTP check: ${HTTP_CODE}"
