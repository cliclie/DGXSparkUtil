#!/bin/sh
# GB10 サーマルガード。root で動作し、nvidia-smi -lgc / -rgc だけを特権で叩く。
# API (dgx-spark-api.service) は cliclie 権限のまま、特権はこの unit にだけ限局する。
#
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
# 終了時は必ず -rgc する。低クロックに固定されたまま性能を失い続ける方が害が大きい。
#
# 全しきい値は環境変数で上書き可能 (EnvironmentFile / systemd unit の Environment=)。

HIGH="${THERMAL_HIGH_C:-90}"
CRIT="${THERMAL_CRIT_C:-98}"
LOW="${THERMAL_LOW_C:-80}"
LOW_SECS="${THERMAL_LOW_SECS:-10}"
LOCK_HIGH="${THERMAL_LOCK_HIGH_MHZ:-1500}"
LOCK_CRIT="${THERMAL_LOCK_CRIT_MHZ:-1000}"
INTERVAL="${THERMAL_INTERVAL_S:-2}"
CONTAINER="${THERMAL_CONTAINER:-vllm-nemotron120b}"
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

trap 'nvidia-smi -rgc >/dev/null 2>&1; exit 0' EXIT TERM INT

current=0
low_count=0
# 起動時は GPU の実状態を知らない。手で -lgc した値 (1475MHz 等) が残っている場合、
# current=0 と実状態が不整合になり want=0 == current=0 で apply が走らず、
# 永久に解除されない。なので起動時に必ず 1 回解除して既知の状態から始める。
nvidia-smi -rgc >/dev/null 2>&1
echo "thermal-guard start: high=${HIGH} crit=${CRIT} low=${LOW}x${LOW_SECS}s lock=${LOCK_HIGH}/${LOCK_CRIT}MHz interval=${INTERVAL}s container=${CONTAINER}" >&2

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
