#!/usr/bin/env bash
# wsl_modelctl_bridge.sh — model-ctl.sh を source して DGXSparkUtil API に機械可読出力を与える
#
# 位置づけ: WSL2 (Whitebear2026) 向けのモデル切替バックエンド (backend=modelctl)。
#   atom1 / atom2 の switch_models.sh と同じ役割を、docker compose を使わず
#   既存の汎用ランチャー model-ctl.sh 1 本で担う。
#
# 重要: model-ctl.sh 本体は一切変更しない。source 後に _mc_ask / _mc_mode_download を
#   上書き定義して非対話化する (Web API からの実行では確認プロンプトを出さない)。
#   対話シェル (~/.bashrc から source した llm / tabby / unloadllm) の挙動にも影響しない。
#
# サブコマンド:
#   list              全モードをタブ区切り 1 行ずつ (8 列、下方の列順参照)
#   status            稼働中モードのみ (list と同じ列)
#   switch <mode>     _mc_launch <mode> (排他停止 → 起動 → 応答待機まで同期実行)
#   stop <mode>       _mc_stop <dir> <label> <port>
#   unload            _mc_unload (稼働中を全て停止)
#   params <mode      表示用パラメータを key<TAB>value 形式で出力
#
# list / status の列順 (タブ区切り):
#   mode  label  port  dir  ready  running  engine  log
#
# 環境変数:
#   DGXUTIL_MODEL_CTL  model-ctl.sh のパス (既定: $HOME/scripts/model-ctl.sh)
#   MC_TIMEOUT         起動完了待機の秒数 (model-ctl.sh 側の既定 600 を継承)

# set -u は使わない: model-ctl.sh は対話シェルでの source を前提として
# PROMPT_COMMAND 等の未設定変数を参照するため、nounset では途中で落ちる。
set -o pipefail

MODEL_CTL="${DGXUTIL_MODEL_CTL:-$HOME/scripts/model-ctl.sh}"
# source 前の安全策 (model-ctl.sh 末尾が PROMPT_COMMAND を参照する)
PROMPT_COMMAND="${PROMPT_COMMAND:-}"

if [ ! -r "$MODEL_CTL" ]; then
  printf 'bridge: model-ctl.sh not readable: %s\n' "$MODEL_CTL" >&2
  exit 3
fi

# shellcheck disable=SC1090
source "$MODEL_CTL" || { echo "bridge: failed to source $MODEL_CTL" >&2; exit 3; }

# --- 非対話化 (API からの実行では確認を聞かない) -----------------------------
# 起動中の他モデルを停止して切り替える = 常に yes。
_mc_ask() { return 0; }
# 重み未導入モードのその場 DL は走らせない (数十 GB の DL を Web 操作で始めない)。
# 呼び出し側 (vllm.py) は list の ready 列を見てボタンを無効化する。
_mc_mode_download() { return 1; }

# --- モード → エンジン種別 (メトリクス取得経路の選択に使う) ------------------
_mc_engine_of() {
    case "$1" in
        exl)                        printf 'exllama' ;;
        tabby|swift40|swift35|qwen9) printf 'tabbyapi' ;;
        strata15|strataS4|strataU4|strataq3|strataC1) printf 'strata' ;;
        llmjp)                      printf 'llamacpp' ;;
        bonsai)                     printf 'llamacpp-docker' ;;
        swiftqwen38|mimo)           printf 'sglang-docker' ;;
        *)                          printf 'unknown' ;;
    esac
}

# --- 1 モード 1 行のダンプ ---------------------------------------------------
_mc_emit_row() {  # <mode>
    local m="$1" st='' run=0
    _mc_mode_up "$m" >/dev/null 2>&1 && run=1
    _mc_mode_ready "$m" >/dev/null 2>&1 && st=1 || st=0
    printf '%s\t%s\t%s\t%s\t%s\t%s\t%s\t%s\n' \
        "$m" \
        "$(_mc_mode_label "$m" | tr '\t' ' ')" \
        "$(_mc_mode_port "$m")" \
        "$(_mc_mode_dir "$m")" \
        "$st" \
        "$run" \
        "$(_mc_engine_of "$m")" \
        "$(_mc_latest_log "$(_mc_mode_dir "$m")")"
}

cmd="${1:-list}"
shift 2>/dev/null || true

case "$cmd" in
  list)
    for m in $_MC_MODES; do _mc_emit_row "$m"; done
    ;;

  status)
    # 稼働中モードを列挙 (Strata は 5 モードが port/process 共有 → 代表 1 件に集約)
    strata_rep=''
    for m in $_MC_MODES; do
        if _mc_is_strata "$m"; then
            [ -z "$strata_rep" ] && strata_rep="$m"
            continue
        fi
        _mc_mode_up "$m" >/dev/null 2>&1 && _mc_emit_row "$m"
    done
    if [ -n "$strata_rep" ] && [ -n "$(_mc_strata_running)" ]; then
        _mc_emit_row "$strata_rep"
    fi
    ;;

  switch)
    mode="${1:-}"
    case " $_MC_MODES " in *" $mode "*) ;; *)
        printf 'bridge: unknown mode: %s\n' "$mode" >&2
        exit 2 ;;
    esac
    _mc_launch "$mode"
    ;;

  stop)
    mode="${1:-}"
    case " $_MC_MODES " in *" $mode "*) ;; *)
        printf 'bridge: unknown mode: %s\n' "$mode" >&2
        exit 2 ;;
    esac
    _mc_stop "$(_mc_mode_dir "$mode")" "$(_mc_mode_label "$mode")" "$(_mc_mode_port "$mode")"
    ;;

  unload)
    _mc_unload
    ;;

  params)
    mode="${1:-}"
    case " $_MC_MODES " in *" $mode "*) ;; *)
        printf 'bridge: unknown mode: %s\n' "$mode" >&2
        exit 2 ;;
    esac
    printf 'mode\t%s\n' "$mode"
    printf 'engine\t%s\n' "$(_mc_engine_of "$mode")"
    printf 'label\t%s\n' "$(_mc_mode_label "$mode" | tr '\t' ' ')"
    printf 'port\t%s\n' "$(_mc_mode_port "$mode")"
    printf 'dir\t%s\n' "$(_mc_mode_dir "$mode")"
    printf 'start_script\t%s\n' "$(_mc_mode_start_script "$mode")"
    printf 'weights_ready\t%s\n' "$(_mc_mode_ready "$mode" >/dev/null 2>&1 && printf yes || printf no)"
    printf 'running\t%s\n' "$(_mc_mode_up "$mode" >/dev/null 2>&1 && printf yes || printf no)"
    printf 'api_key\t%s\n' "$(_mc_mode_key "$mode")"
    # Cline 設定表示用: WSL 内では mDNS (.local) が解決できないため LAN IP を返す
    # (model-ctl.sh の _mc_show_info と同じ抽出方法)
    printf 'lan_ip\t%s\n' "$(ip -4 addr show 2>/dev/null | \
        awk '$1=="inet" && $NF!="lo" && /scope global/ && $2 !~ /^127/ {sub(/\/.*/,"",$2); print $2; exit}')"
    ctx=$(_mc_mode_ctx_conf "$mode")
    [ -n "$ctx" ] && printf 'context_length\t%s\n' "$ctx"
    mo=$(_mc_mode_maxout_conf "$mode")
    [ -n "$mo" ] && printf 'max_output_tokens\t%s\n' "$mo"
    # Strata は設定 JSON から追加項目を読む (port / context は JSON 側が正本)
    if _mc_is_strata "$mode"; then
        cfg=$(_mc_strata_cfg_of "$mode")
        printf 'config_json\t%s\n' "$cfg"
        printf 'model_name\t%s\n' "$(_mc_strata_id "$mode")"
    fi
    ;;

  *)
    printf 'usage: %s {list|status|switch <mode>|stop <mode>|unload|params <mode>}\n' \
        "$(basename "$0")" >&2
    exit 2
    ;;
esac