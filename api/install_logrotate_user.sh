#!/usr/bin/env bash
# server.log の日次 truncate を **sudo 不要** で有効化する (systemd user unit)
#
#   dgx-spark-api.service (system unit) が書く server.log は cliclie 所有なので、
#   同じ cliclie 権限の user unit で truncate できる (特権は要らない)。
#   Linger を有効化するので、ログインなし (ブート直後) も user manager が起動し、
#   timer は JST 0 時に発火する。
#
# 使い方:  bash <repo>/api/install_logrotate_user.sh
#   解除:  systemctl --user disable --now dgx-spark-logrotate.timer
#          rm ~/.config/systemd/user/dgx-spark-logrotate.{service,timer}
#          systemctl --user daemon-reload
#
# 同一マシンで `sudo bash api/install_service.sh` (system unit 版) を後に実行しても、
# rotate_log.sh の flock により後発は何もしないので二重実行しない。
# system unit 版を入れたら、こちらの user timer は解除する (下記「解除」)。
set -euo pipefail

API_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
UNIT_DIR="${HOME}/.config/systemd/user"
LR_SERVICE="dgx-spark-logrotate.service"
LR_TIMER="dgx-spark-logrotate.timer"
RUN_USER="$(id -un)"

# install_service.sh と同じ JST 換算。OnCalendar はマシンのローカル TZ で評価される
# (unit の TZ 環境変数は ExecStart にしか効かない) ため、JST 0 時をローカル時刻に直す。
JST_MIDNIGHT_EPOCH="$(TZ=Asia/Tokyo date -d 'today 00:00:00' +%s)"
JST_MIDNIGHT_LOCAL="*-*-* $(date -d "@${JST_MIDNIGHT_EPOCH}" +%H:%M:%S)"

chmod 755 "${API_DIR}/rotate_log.sh"
mkdir -p "${UNIT_DIR}"
echo "==> Rendering user units -> ${UNIT_DIR}/${LR_SERVICE} / ${LR_TIMER} (OnCalendar='${JST_MIDNIGHT_LOCAL}')"
# user manager では User=/Group= は使えず、Nice=/IOSchedulingClass= も無視されるので
# system unit テンプレートから該当行を落として書き出す
sed -e "s|__API_DIR__|${API_DIR}|g" -e "s|__USER__|${RUN_USER}|g" \
  -e '/^User=/d' -e '/^Group=/d' -e '/^Nice=/d' -e '/^IOSchedulingClass=/d' \
  "${API_DIR}/${LR_SERVICE}" > "${UNIT_DIR}/${LR_SERVICE}"
sed -e "s|__API_DIR__|${API_DIR}|g" -e "s|__USER__|${RUN_USER}|g" \
  -e "s|__JST_MIDNIGHT_LOCAL__|${JST_MIDNIGHT_LOCAL}|g" \
  "${API_DIR}/${LR_TIMER}" > "${UNIT_DIR}/${LR_TIMER}"

systemctl --user daemon-reload
systemctl --user enable --now "${LR_TIMER}"

# user manager をログインなしで起動させる (有効化しないとブート後に timer が死んでいる)
if [ "$(loginctl show-user "${RUN_USER}" -p Linger 2>/dev/null | cut -d= -f2)" != "yes" ]; then
  echo "==> Enabling linger (user manager をブート後も起動)"
  loginctl enable-linger "${RUN_USER}" \
    || echo "WARN: loginctl enable-linger に失敗。手動で 'sudo loginctl enable-linger ${RUN_USER}' を実行してください"
fi

echo "==> Running the first truncate now"
systemctl --user start "${LR_SERVICE}"
systemctl --user list-timers "${LR_TIMER}" --no-pager | head -n 3
