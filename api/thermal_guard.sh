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
# 対象は THERMAL_CONTAINERS (空白区切りリスト) の LLM が稼働中の時だけ。既定は
# vllm-nemotron120b と tabbyapi-ornith122b。2026-10-16 は ExLlama 系 (Qwen 4p05) が
# 259K prefill で 88℃/71W と HIGH(90) に届かず圏外だったが、2026-10-28 に Ornith 122B の
# 259,040 tok prefill で acpitz 97℃ を実測したため ExLlama 系も対象に含めた。
# 終了時は必ず -rgc する。低クロックに固定されたまま性能を失い続ける方が害が大きい。
#
# --- clock groups (2026-11-01 案A / THERMAL_GROUPS=1) ---------------------------
# モデル (コンテナ名) を問わず常時監視し、CPU 温度と GPU 温度を別々のセンサー・別々の
# 閾値で見て、それぞれが GPU クロックと CPU クロックを絞る。発動は OR、解除は AND。
# 実測根拠:
#   2026-10-16 hw_watch 240,273 サンプル: acpitz が nvidia-smi GPU 温度を平均 4.9℃・
#   最大 30℃ 上回る (253/254 で CPU 側が高い)。nvidia-smi の最大は 85℃ なので、
#   GPU 側に 90℃ を置くと永久に発動しない。
#   ガード非動作時に acpitz 97℃ を実測 (2026-10-28 Ornith 122B 259K prefill)。
#   本体は acpitz 104℃ で落ちる。上昇 0.4℃/秒。
# 段:
#   CPU 群 = max(TSOC,TS0E,TS0P,TS1E,TS1P,TUNC)   ※TGPU を除く
#     >= CPU_CRIT(93)  -> 大核 2400 / 小核 1800 MHz   (104℃ まで 11℃ の余裕)
#     >= CPU_HIGH(88)  -> 大核 3200 / 小核 2400 MHz
#   GPU 群 = max(TGPU, nvidia-smi temperature.gpu)
#     >= GPU_CRIT(90)  -> -lgc 1000MHz   (NVML GPUShutdownTempThreshold=90 と一致)
#     >= GPU_HIGH(84)  -> -lgc 1665MHz   (実効 1644MHz。10/16 に acpitz 90℃ 頭打ちを実測)
#   解除は CPU_LOW(82) と GPU_LOW(78) の両方へ LOW_SECS(10) 秒継続して初めて。
# CPU 群 / GPU 群の区別は device/path の末尾名 (\_TZ_.TSOC 等) で行う。
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
# 対象コンテナは空白区切りのリスト (docker ps の名前と完全一致)。
# THERMAL_CONTAINER (単一指定) は後方互換として残し、指定があればそれを優先する。
# 展開は ${VAR-default} (コロン無し) にする。WSL2 の「監視専ら」導入は
# install_service.sh が unit へ Environment=THERMAL_CONTAINERS= (空文字) を注入して
# 発動対象を無くすが、${VAR:-default} だと空文字も既定リストへフォールバックして
# 対象が残ってしまう (2026-10-30 に実測: containers=vllm-nemotron120b ... と起動ログ)。
# 未設定 (atom1 / atom2 の既定) は従来どおり既定リストを使う。
CONTAINERS="${THERMAL_CONTAINERS-vllm-nemotron120b tabbyapi-ornith122b}"
[ -z "${THERMAL_CONTAINER:-}" ] || CONTAINERS="$THERMAL_CONTAINER"
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

# --- clock groups (2026-11-01 案A: CPU 温度と GPU 温度を別々に監視) -------------
# THERMAL_GROUPS=1 で有効。既定 0 = 従来の単一ヒステリシス (max(acpitz, GPU温度)) を
# そのまま維持するので atom2 ほか既存機は無影響。WhitebearATOM1 の unit だけで有効化し、
# 監視はモデル (コンテナ名) を問わない常時監視にする。
GROUPS="${THERMAL_GROUPS:-0}"
# --- CPU 側: acpitz のうち TGPU を除く zone の最大 ------------------------------
CPU_HIGH="${THERMAL_CPU_HIGH_C:-88}"
CPU_CRIT="${THERMAL_CPU_CRIT_C:-93}"
CPU_LOW="${THERMAL_CPU_LOW_C:-82}"
# 大核 (policy5-9,15-19 / cpuinfo_max_freq 3900000) と小核 (policy0-4,10-14 / 2808000)
# で段を分ける。cpuinfo_max_freq >= CPU_FAST_MIN_KHZ を大核とみなす。
CPU_LOCK_HIGH="${THERMAL_CPU_LOCK_HIGH_KHZ:-3200000}"
CPU_LOCK_CRIT="${THERMAL_CPU_LOCK_CRIT_KHZ:-2400000}"
CPU_SMALL_LOCK_HIGH="${THERMAL_CPU_SMALL_LOCK_HIGH_KHZ:-2400000}"
CPU_SMALL_LOCK_CRIT="${THERMAL_CPU_SMALL_LOCK_CRIT_KHZ:-1800000}"
CPU_FAST_MIN="${THERMAL_CPU_FAST_MIN_KHZ:-3500000}"
# --- GPU 側: acpitz の TGPU と nvidia-smi temperature.gpu の最大 ---------------
GPU_HIGH="${THERMAL_GPU_HIGH_C:-84}"
GPU_CRIT="${THERMAL_GPU_CRIT_C:-90}"
GPU_LOW="${THERMAL_GPU_LOW_C:-78}"
GPU_LOCK_HIGH="${THERMAL_GPU_LOCK_HIGH_MHZ:-1665}"
GPU_LOCK_CRIT="${THERMAL_GPU_LOCK_CRIT_MHZ:-1000}"
# acpitz の zone は type が 7 つ全部 "acpitz" なので区別できない。device/path
# (\_TZ_.TSOC 等) の末尾名で判別する。index 依存は ACPI 列挙順に依存して壊れるため使わない。
CPU_ZONES="${THERMAL_CPU_ACPITZ_ZONES:-TSOC TS0E TS0P TS1E TS1P TUNC}"
GPU_ZONES="${THERMAL_GPU_ACPITZ_ZONES:-TGPU}"

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

# ---------------------------------------------------------------- groups 用センサー
zone_name() {  # $1 = zone dir。device/path (\_TZ_.TSOC) の末尾名を返す
  p="$(cat "$1/device/path" 2>/dev/null)"
  echo "${p##*.}"
}

acpitz_group_max_c() {  # $1 = zone 名リスト。1 つも一致しなければ全 acpitz の最大に退避
  max=0
  found=0
  for z in /sys/class/thermal/thermal_zone*; do
    [ "$(cat "$z/type" 2>/dev/null)" = "acpitz" ] || continue
    n="$(zone_name "$z")"
    hit=0
    for w in $1; do
      [ "$n" = "$w" ] && { hit=1; break; }
    done
    [ "$hit" -eq 1 ] || continue
    t="$(cat "$z/temp" 2>/dev/null)"
    [ -n "$t" ] || continue
    found=1
    c=$((t / 1000))
    [ "$c" -gt "$max" ] && max=$c
  done
  [ "$found" -eq 1 ] || { acpitz_max_c; return; }
  echo "$max"
}

gpu_group_max_c() {  # max(acpitz の TGPU, nvidia-smi temperature.gpu)
  a="$(acpitz_group_max_c "$GPU_ZONES")"
  g="$(gpu_query temperature.gpu)"
  [ -n "$g" ] || g=0
  [ "$g" -gt "$a" ] && echo "$g" || echo "$a"
}

gpu_want_mhz() {  # $1 = GPU 温度。0 は解除
  [ "$1" -ge "$GPU_CRIT" ] && { echo "$GPU_LOCK_CRIT"; return; }
  [ "$1" -ge "$GPU_HIGH" ] && { echo "$GPU_LOCK_HIGH"; return; }
  echo 0
}

cpu_want_khz() {  # $1 = CPU 温度。出力は "<大核 kHz> <小核 kHz>"。0 は解除
  [ "$1" -ge "$CPU_CRIT" ] && { echo "$CPU_LOCK_CRIT $CPU_SMALL_LOCK_CRIT"; return; }
  [ "$1" -ge "$CPU_HIGH" ] && { echo "$CPU_LOCK_HIGH $CPU_SMALL_LOCK_HIGH"; return; }
  echo "0 0"
}

# ---------------------------------------------------------------- groups 用 CPU クロック
cpu_policies() { ls -d /sys/devices/system/cpu/cpufreq/policy* 2>/dev/null; }

apply_cpu() {  # $1 = 大核 kHz, $2 = 小核 kHz。cpuinfo_max_freq で上限クランプする
  for p in $(cpu_policies); do
    mx="$(cat "$p/cpuinfo_max_freq" 2>/dev/null)"
    [ -n "$mx" ] || continue
    if [ "$mx" -ge "$CPU_FAST_MIN" ]; then v="$1"; else v="$2"; fi
    [ "$v" -gt "$mx" ] && v="$mx"
    [ "$v" -gt 0 ] || continue
    f="$p/scaling_max_freq"
    # 書き込み不可の policy を素通しすると dash がリダイレクト失敗を自前で行に吐き、
    # journal が 20 行 x 2 秒で埋まる。unit は root なので通常は常に書き込み可。
    [ -w "$f" ] || continue
    echo "$v" > "$f" 2>/dev/null
  done
}

reset_cpu() {  # 全 policy を cpuinfo_max_freq に戻す (低クロック固定を残さない)
  for p in $(cpu_policies); do
    mx="$(cat "$p/cpuinfo_max_freq" 2>/dev/null)"
    [ -n "$mx" ] || continue
    f="$p/scaling_max_freq"
    [ -w "$f" ] || continue
    echo "$mx" > "$f" 2>/dev/null
  done
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
    [ "$GROUPS" = 1 ] && reset_cpu
  fi
  exit 0
}
trap on_exit EXIT TERM INT

# ------------------------------------------------- clock (GB10) groups (案A 2026-11-01)
# CPU 温度 (acpitz の TGPU 以外) と GPU 温度 (TGPU と nvidia-smi の max) を別々に監視し、
# それぞれの段で GPU クロック (-lgc) と CPU クロック (scaling_max_freq) を絞る。
# 発動は OR (どちらか一方でも HIGH 以上で即発動)、解除は AND (両方が LOW 以下を
# LOW_SECS 秒継続)。コンテナ名による稼働判定は行わない = モデルを問わず常時監視。
loop_clock_groups() {
  cur_gpu=0
  cur_cpu_f=0
  cur_cpu_s=0
  low_count=0
  # 起動時は実状態を知らない (手動 -lgc / scaling_max_freq の残留)。必ず解除して始める。
  nvidia-smi -rgc >/dev/null 2>&1
  reset_cpu
  echo "thermal-guard start: mode=clock groups=1 cpu=${CPU_HIGH}/${CPU_CRIT}/${CPU_LOW}C lock=${CPU_LOCK_HIGH}/${CPU_LOCK_CRIT}kHz small=${CPU_SMALL_LOCK_HIGH}/${CPU_SMALL_LOCK_CRIT}kHz gpu=${GPU_HIGH}/${GPU_CRIT}/${GPU_LOW}C lock=${GPU_LOCK_HIGH}/${GPU_LOCK_CRIT}MHz low_secs=${LOW_SECS} interval=${INTERVAL}s zones_cpu=${CPU_ZONES} zones_gpu=${GPU_ZONES}" >&2

  while :; do
    a="$(acpitz_group_max_c "$CPU_ZONES")"
    g="$(gpu_group_max_c)"

    set -- $(cpu_want_khz "$a")
    wf="$1"
    ws="$2"
    wg="$(gpu_want_mhz "$g")"

    if [ "$wg" -gt 0 ] || [ "$wf" -gt 0 ]; then
      # 発動中。自セグメントが LOW を超えている間は解除要求を無視 (ヒステリシス)
      low_count=0
      if [ "$wg" -eq 0 ] && [ "$g" -gt "$GPU_LOW" ]; then wg="$cur_gpu"; fi
      if [ "$wf" -eq 0 ] && [ "$a" -gt "$CPU_LOW" ]; then wf="$cur_cpu_f"; ws="$cur_cpu_s"; fi
    elif [ "$a" -le "$CPU_LOW" ] && [ "$g" -le "$GPU_LOW" ]; then
      # 両群が LOW 以下。LOW_SECS 秒続いて初めて解除
      low_count=$((low_count + 1))
      if [ "$low_count" -lt "$((LOW_SECS / INTERVAL))" ]; then
        wg="$cur_gpu"; wf="$cur_cpu_f"; ws="$cur_cpu_s"
      fi
    else
      # 片方が LOW を超えている = 解除条件 (AND) を満たさない。現状維持
      low_count=0
      wg="$cur_gpu"; wf="$cur_cpu_f"; ws="$cur_cpu_s"
    fi

    if [ "$wg" -ne "$cur_gpu" ]; then
      apply "$wg"
      echo "thermal-guard: GPU ${g}C -> ${wg}MHz (cpu=${a}C)" >&2
      cur_gpu="$wg"
    fi
    if [ "$wf" -ne "$cur_cpu_f" ] || [ "$ws" -ne "$cur_cpu_s" ]; then
      if [ "$wf" -eq 0 ]; then reset_cpu; else apply_cpu "$wf" "$ws"; fi
      echo "thermal-guard: CPU ${a}C -> ${wf}/${ws}kHz (gpu=${g}C)" >&2
      cur_cpu_f="$wf"
      cur_cpu_s="$ws"
    fi

    t="$a"
    [ "$g" -gt "$t" ] && t="$g"
    hc="$CPU_HIGH"; [ "$GPU_HIGH" -lt "$hc" ] && hc="$GPU_HIGH"
    cc="$CPU_CRIT"; [ "$GPU_CRIT" -lt "$cc" ] && cc="$GPU_CRIT"
    lc="$CPU_LOW";  [ "$GPU_LOW" -lt "$lc" ] && lc="$GPU_LOW"
    printf '{"ts":%s,"enabled":true,"mode":"clock","groups":true,"temp_c":%s,"acpitz_c":%s,"gpu_c":%s,"clock_mhz":%s,"locked_mhz":%s,"cpu_locked_khz":%s,"cpu_locked_small_khz":%s,"model_running":1,"low_count":%s,"high_c":%s,"crit_c":%s,"low_c":%s,"lock_high_mhz":%s,"lock_crit_mhz":%s,"cpu_high_c":%s,"cpu_crit_c":%s,"cpu_low_c":%s,"cpu_lock_high_khz":%s,"cpu_lock_crit_khz":%s,"gpu_high_c":%s,"gpu_crit_c":%s,"gpu_low_c":%s,"gpu_lock_high_mhz":%s,"gpu_lock_crit_mhz":%s}\n' \
      "$(date +%s)" "$t" "$a" "$g" "$(gpu_query clocks.sm)" "$cur_gpu" "$cur_cpu_f" "$cur_cpu_s" \
      "$low_count" "$hc" "$cc" "$lc" "$GPU_LOCK_HIGH" "$GPU_LOCK_CRIT" \
      "$CPU_HIGH" "$CPU_CRIT" "$CPU_LOW" "$CPU_LOCK_HIGH" "$CPU_LOCK_CRIT" \
      "$GPU_HIGH" "$GPU_CRIT" "$GPU_LOW" "$GPU_LOCK_HIGH" "$GPU_LOCK_CRIT" > "$STATE" 2>/dev/null

    sleep "$INTERVAL"
  done
}

# ---------------------------------------------------------------- clock (GB10)
loop_clock() {
  current=0
  low_count=0
  # 起動時は GPU の実状態を知らない。手で -lgc した値 (1475MHz 等) が残っている場合、
  # current=0 と実状態が不整合になり want=0 == current=0 で apply が走らず、
  # 永久に解除されない。なので起動時に必ず 1 回解除して既知の状態から始める。
  nvidia-smi -rgc >/dev/null 2>&1
  echo "thermal-guard start: mode=clock high=${HIGH} crit=${CRIT} low=${LOW}x${LOW_SECS}s lock=${LOCK_HIGH}/${LOCK_CRIT}MHz interval=${INTERVAL}s containers=${CONTAINERS}" >&2

  while :; do
    a="$(acpitz_max_c)"
    g="$(gpu_query temperature.gpu)"
    [ -n "$g" ] || g=0
    t="$a"
    [ "$g" -gt "$t" ] && t="$g"

    running=0
    names="$(docker ps --format '{{.Names}}' 2>/dev/null)"
    for c in $CONTAINERS; do
      echo "$names" | grep -qx "$c" && { running=1; break; }
    done

    want="$(target_mhz "$t")"
    if [ "$running" -eq 0 ]; then
      # 対象リスト (CONTAINERS) 外のときは絞らない。対象は既定で nemotron と
      # tabbyapi-ornith122b (2026-10-28 に 259K prefill で acpitz 97℃ を実測して追加)
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

if [ "$MODE" = power ]; then
  loop_power
elif [ "$GROUPS" = 1 ]; then
  loop_clock_groups
else
  loop_clock
fi
