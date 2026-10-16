#!/bin/sh
# GPU サーマルガード。root で動作し、GPU 制限の書き込みだけを特権で叩く。
# API (dgx-spark-api.service) は cliclie 権限のまま、特権はこの unit にだけ限局する。
#
# モードは起動時に自動判定 (THERMAL_MODE=clock|power で上書き可能)。両機同一スクリプト。
#   clock = NVIDIA GB10 (atom1)。nvidia-smi -lgc / -rgc で GPU クロックを絞る
#   power = AMD R9700  (atom2)。sysfs power1_cap で GPU 電力上限を絞る
#
# --- clock (GB10) ---------------------------------------------------------------
# 背景: GB10 は CPU/GPU 同一ダイ共有冷却で、acpitz が 104℃ に達すると本体が落ちる。
# 実測(2026-10-16): GPU 2398MHz で 196K prefill 時 acpitz 94℃、1475MHz にすると
# 68〜72℃ で飽和し 254K が通る。GPU 電力は 83W → 26W。
# 温度上昇は 0.4℃/秒、80℃ 検知から 90℃ 到達まで 24 秒あるので 2 秒制御で間に合う。
#
# 制御: max(acpitz, GPU温度) を 3 段ヒステリシスで見る。
#   >= CRIT(98)      -> LOCK_CRIT(1000)MHz   104℃ 停止への最後の弁
#   >= HIGH(90)      -> LOCK_HIGH(1500)MHz
#   <= LOW(80) が LOW_SECS(10)秒 継続 -> -rgc で解除
# 下げるのは即時、上げるのは 10 秒継続。GB10 の熱時定数が約 100 秒あるので、
# 対称な条件だと発振する。
# 対象は THERMAL_CONTAINER の LLM が稼働中の時だけ (ExLlama 系は圏外)。
# 終了時は必ず -rgc する。低クロックに固定されたまま性能を失い続ける方が害が大きい。
#
# --- power (R9700) --------------------------------------------------------------
# 背景: 従来は llm-gpu-powercap.service (oneshot) が power1_cap を 210W に常時固定して
# いた。80℃ 未満でも性能を捨ててしまうので、GPU 温度に応じて 3 段で動かす。
# 監視するのは GPU 温度 (label=junction) のみ。ロード中の LLM は問わない (常時監視)。
#   > PWR_CRIT(90)℃  -> CAP_CRIT(210)W   (sysfs power1_cap_min = 書ける下限)
#   > PWR_HIGH(80)℃  -> CAP_HIGH(250)W
#   それ以外          -> CAP_MAX(300)W    (sysfs power1_cap_max = 最大)
# 電力を「下げる」方向は即時、「上げる」方向 (解放) は RELEASE_SECS(2)秒 継続でのみ適用。
# 終了時は必ず CAP_MAX へ復帰する (clock の -rgc と同じ理由)。
#
# 全しきい値は環境変数で上書き可能 (EnvironmentFile / systemd unit の Environment=)。

# ---------------------------------------------------------------- モード判定
# clock: nvidia-smi が叩ける (GB10)。power: amdgpu の power1_cap sysfs がある (R9700)。
# THERMAL_MODE=clock|power で明示上書き (誤検出の保険)。
detect_mode() {
  case "${THERMAL_MODE:-}" in
    clock) echo clock; return ;;
    power) echo power; return ;;
  esac
  if command -v nvidia-smi >/dev/null 2>&1; then echo clock; return; fi
  for f in /sys/class/drm/card[0-9]*/device/hwmon/hwmon*/power1_cap; do
    [ -e "$f" ] && { echo power; return; }
  done
  echo clock
}
MODE="$(detect_mode)"

# --- clock (GB10 / nvidia-smi -lgc) ---------------------------------------------
HIGH="${THERMAL_HIGH_C:-90}"
CRIT="${THERMAL_CRIT_C:-98}"
LOW="${THERMAL_LOW_C:-80}"
LOW_SECS="${THERMAL_LOW_SECS:-10}"
LOCK_HIGH="${THERMAL_LOCK_HIGH_MHZ:-1500}"
LOCK_CRIT="${THERMAL_LOCK_CRIT_MHZ:-1000}"
CONTAINER="${THERMAL_CONTAINER:-vllm-nemotron120b}"
# 監視間隔: GB10 の温度上昇は 0.4℃/秒なので 2 秒で十分。R9700 は電力切替の応答が
# 速いので 1 秒 (解放の 2 秒判定を 2 サンプルで見る)。
INTERVAL="${THERMAL_INTERVAL_S:-2}"

# --- power (R9700 / sysfs power1_cap) -------------------------------------------
PWR_HIGH="${THERMAL_POWER_HIGH_C:-80}"    # この温度を超えたら CAP_HIGH へ
PWR_CRIT="${THERMAL_POWER_CRIT_C:-90}"    # この温度を超えたら CAP_CRIT へ
CAP_HIGH="${THERMAL_CAP_HIGH_W:-250}"
CAP_CRIT="${THERMAL_CAP_CRIT_W:-210}"
CAP_MAX="${THERMAL_CAP_MAX_W:-300}"       # 80℃ 以下の上限。power1_cap_max でクランプ
RELEASE_SECS="${THERMAL_POWER_RELEASE_SECS:-2}"
if [ "$MODE" = power ]; then
  INTERVAL="${THERMAL_INTERVAL_S:-1}"
fi

STATE="${THERMAL_STATE_FILE:-/run/thermal/state}"

acpitz_max_c() {
  max=0
  for z in /sys/class/thermal/thermal_zone*; do
    [ "$(cat "$z/type" 2>/dev/null)" = "acpitz" ] || continue
    t="$(cat "$z/temp" 2>/dev/null)"
    [ -n "$t" ] || continue
    c=$((t / 1000))
    [ "$c" -gt "$max" ] && max=$c
  done
  echo "$max"
}

gpu_query() {  # $1 = query field
  nvidia-smi "--query-gpu=$1" --format=csv,noheader,nounits 2>/dev/null | head -1
}

target_mhz() {  # $1 = 温度。0 は解除
  [ "$1" -ge "$CRIT" ] && { echo "$LOCK_CRIT"; return; }
  [ "$1" -ge "$HIGH" ] && { echo "$LOCK_HIGH"; return; }
  echo 0
}

apply() {  # $1 = 狙い MHz。0 は解除
  if [ "$1" -eq 0 ]; then
    nvidia-smi -rgc >/dev/null 2>&1
  else
    nvidia-smi -lgc "$1,$1" >/dev/null 2>&1
  fi
}

# ---------------------------------------------------------------- power 用 (R9700)
cap_file() {  # amdgpu の power1_cap パス。name=amdgpu の hwmon を優先する
  for h in /sys/class/drm/card[0-9]*/device/hwmon/hwmon*; do
    [ -e "$h/power1_cap" ] || continue
    [ "$(cat "$h/name" 2>/dev/null)" = "amdgpu" ] && { echo "$h/power1_cap"; return 0; }
  done
  for f in /sys/class/drm/card[0-9]*/device/hwmon/hwmon*/power1_cap; do
    [ -e "$f" ] && { echo "$f"; return 0; }
  done
  return 1
}

read_int() {  # $1 = ファイル。読めなければ 0
  v="$(cat "$1" 2>/dev/null)"
  [ -n "$v" ] || v=0
  echo "$v"
}

gpu_temp_mc() {  # $1 = hwmon ディレクトリ。GPU 温度をミリ℃で返す。
  # label=junction を優先 (metrics.py:_amdgpu_temp_c と同じ定義)。無ければ計測最高値。
  best=0
  junc=-1
  for t in "$1"/temp*_input; do
    [ -e "$t" ] || continue
    v="$(read_int "$t")"
    [ "$v" -gt "$best" ] && best="$v"
    [ "$(cat "${t%_input}_label" 2>/dev/null)" = "junction" ] || continue
    [ "$v" -gt "$junc" ] && junc="$v"
  done
  [ "$junc" -ge 0 ] && { echo "$junc"; return; }
  echo "$best"
}

want_w() {  # $1 = GPU温度 (ミリ℃)。狙いの電力上限 W。
            # 「80℃ を超えたら」「90℃ を超えたら」なので境界値自体は上位段 (ミリ℃で比較)
  [ "$1" -gt "$((PWR_CRIT * 1000))" ] && { echo "$CAP_CRIT"; return; }
  [ "$1" -gt "$((PWR_HIGH * 1000))" ] && { echo "$CAP_HIGH"; return; }
  echo "$CAP_MAX"
}

apply_w() {  # $1 = 狙い W。sysfs は µW (dash に 10# 進数指定は無いので素の算術展開)
  echo $(($1 * 1000000)) > "$CAPF" 2>/dev/null
}

on_exit() {
  if [ "$MODE" = power ]; then
    [ -n "$CAPF" ] && echo "$((CAP_MAX * 1000000))" > "$CAPF" 2>/dev/null
  else
    nvidia-smi -rgc >/dev/null 2>&1
  fi
  exit 0
}
trap on_exit EXIT TERM INT

# ---------------------------------------------------------------- clock (GB10)
loop_clock() {
  current=0
  low_count=0
  # 起動時は GPU の実状態を知らない。手で -lgc した値 (1475MHz 等) が残っている場合、
  # current=0 と実状態が不整合になり want=0 == current=0 で apply が走らず、
  # 永久に解除されない。なので起動時に必ず 1 回解除して既知の状態から始める。
  nvidia-smi -rgc >/dev/null 2>&1
  echo "thermal-guard start: mode=clock high=${HIGH} crit=${CRIT} low=${LOW}x${LOW_SECS}s lock=${LOCK_HIGH}/${LOCK_CRIT}MHz interval=${INTERVAL}s container=${CONTAINER}" >&2

  while :; do
    a="$(acpitz_max_c)"
    g="$(gpu_query temperature.gpu)"
    [ -n "$g" ] || g=0
    t="$a"
    [ "$g" -gt "$t" ] && t="$g"

    running=0
    docker ps --format '{{.Names}}' 2>/dev/null | grep -qx "$CONTAINER" && running=1

    want="$(target_mhz "$t")"
    if [ "$running" -eq 0 ]; then
      # nemotron 以外 (ExLlama 系は CPU/RAM 主体で 88℃/71W 圏外) は絞らない
      want=0
      low_count=0
    elif [ "$want" -gt 0 ]; then
      low_count=0
    elif [ "$t" -gt "$LOW" ]; then
      # LOW と HIGH の間 = ヒステリシス帯域。ここでの解除は発振する (90℃ で下げる
      # → 84℃ で解除 → 即 90℃ → 発動 の 10 秒ループ)。locked を維持する。
      want="$current"
      low_count=0
    else
      low_count=$((low_count + 1))
      [ "$low_count" -ge "$((LOW_SECS / INTERVAL))" ] || want="$current"
    fi

    if [ "$want" -ne "$current" ]; then
      apply "$want"
      echo "thermal-guard: ${t}C -> ${want}MHz (acpitz=${a}C gpu=${g}C running=${running})" >&2
      current="$want"
    fi

    printf '{"ts":%s,"enabled":true,"temp_c":%s,"acpitz_c":%s,"gpu_c":%s,"clock_mhz":%s,"locked_mhz":%s,"model_running":%s,"low_count":%s,"high_c":%s,"crit_c":%s,"low_c":%s,"lock_high_mhz":%s,"lock_crit_mhz":%s}\n' \
      "$(date +%s)" "$t" "$a" "$g" "$(gpu_query clocks.sm)" "$current" "$running" \
      "$low_count" "$HIGH" "$CRIT" "$LOW" "$LOCK_HIGH" "$LOCK_CRIT" > "$STATE" 2>/dev/null

    sleep "$INTERVAL"
  done
}

# ---------------------------------------------------------------- power (R9700)
# GPU 温度 (junction) だけを見て power1_cap を 3 段で動かす。ロード中の LLM は
# 問わない (常時監視)。clock モードのような docker ps 判定は行わない。
loop_power() {
  CAPF="$(cap_file)" || { echo "thermal-guard: amdgpu power1_cap not found" >&2; exit 1; }
  HWMON="$(dirname "$CAPF")"
  cap_min=$(( $(read_int "$HWMON/power1_cap_min") / 1000000 ))
  cap_max_hw=$(( $(read_int "$HWMON/power1_cap_max") / 1000000 ))
  # sysfs の書ける範囲外を指定すると書き込みが失敗してそのまま固定される
  [ "$CAP_CRIT" -lt "$cap_min" ] && CAP_CRIT="$cap_min"
  [ "$CAP_MAX" -gt "$cap_max_hw" ] && CAP_MAX="$cap_max_hw"

  # 起動時は実状態を知らない (旧 llm-gpu-powercap.service が 210W に固定している等)。
  # 必ず最大へ戻して既知の状態から始める。
  apply_w "$CAP_MAX"
  current="$CAP_MAX"
  pending=0
  pending_since=0
  hold_s=0
  echo "thermal-guard start: mode=power high=${PWR_HIGH} crit=${PWR_CRIT} caps=${CAP_MAX}/${CAP_HIGH}/${CAP_CRIT}W release=${RELEASE_SECS}s interval=${INTERVAL}s cap=${CAPF}" >&2

  while :; do
    now="$(date +%s)"
    mt="$(gpu_temp_mc "$HWMON")"
    t=$((mt / 1000))
    want="$(want_w "$mt")"

    if [ "$want" -eq "$current" ]; then
      pending=0
      hold_s=0
    elif [ "$want" -lt "$current" ]; then
      # 電力を下げる (制限を強める) 方向は即時
      apply_w "$want"
      echo "thermal-guard: ${t}C -> ${want}W (was ${current}W)" >&2
      current="$want"
      pending=0
      hold_s=0
    else
      # 電力を上げる (解放) 方向は、その狙い値が RELEASE_SECS 秒「以上」続いた時だけ
      # 適用する。サンプル数ではなく実時刻で見るので、監視間隔の実測ぶん
      # (sysfs 読み + sleep で 1 秒をやや超える) に引きずられない。
      # 段階を跨ぐ解放も 2 秒続いた狙い値にしか行わないので、89℃⇄91℃ の発振で
      # 210↔250W を連打することはない
      if [ "$want" -ne "$pending" ]; then
        pending="$want"
        pending_since="$now"
      fi
      hold_s=$((now - pending_since))
      if [ "$hold_s" -ge "$RELEASE_SECS" ]; then
        apply_w "$want"
        echo "thermal-guard: ${t}C -> ${want}W (was ${current}W, released after ${hold_s}s)" >&2
        current="$want"
        pending=0
        hold_s=0
      fi
    fi

    pw=$(( $(read_int "$HWMON/power1_average") / 1000000 ))
    printf '{"ts":%s,"enabled":true,"mode":"power","temp_c":%d.%03d,"power_cap_w":%s,"power_max_w":%s,"power_min_w":%s,"power_avg_w":%s,"always_monitor":true,"hold_s":%s,"high_c":%s,"crit_c":%s,"cap_high_w":%s,"cap_crit_w":%s,"cap_max_w":%s,"release_secs":%s}\n' \
      "$now" "$((mt / 1000))" "$((mt % 1000))" "$current" "$CAP_MAX" "$cap_min" \
      "$pw" "$hold_s" "$PWR_HIGH" "$PWR_CRIT" "$CAP_HIGH" "$CAP_CRIT" "$CAP_MAX" \
      "$RELEASE_SECS" > "$STATE" 2>/dev/null

    sleep "$INTERVAL"
  done
}

case "$MODE" in
  power) loop_power ;;
  *) loop_clock ;;
esac
