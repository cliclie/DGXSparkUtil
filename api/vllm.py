"""vLLM 状態モニタリング / モデル切替 / パラメータ編集。

- 状態: docker ps/inspect + vLLM /health, /v1/models, /metrics + docker logs
- プロファイル対応表: docker-compose.yml を動的にパース(モデル追加に自動追従)
- 切替: 既存スクリプト switch_models.sh をバックグラウンド実行
- パラメータ編集: docker-compose.yml の command を書き換えて --force-recreate

バックエンドは config.BACKEND で分岐する (実装メモ 2026-10-21):
- "compose"  : 上記の従来動作 (dgx-spark / atom2)。このモジュールのロジックは不変
- "modelctl" : WSL2。docker を使わず、汎用ランチャー model-ctl.sh を source した
               ブリッジ (wsl_modelctl_bridge.sh / modelctl.py) から同一形状の
               プロファイル表・状態・メトリクスを取得する。切替は _mc_launch を
               バックグラウンドジョブとして実行する。
"""

from __future__ import annotations

import json
import re
import socket
import subprocess
import time
from pathlib import Path

import config
import modelctl

# パス・排他有無は config (プラットフォーム検出) に集約
COMPOSE_DIR = config.COMPOSE_DIR
COMPOSE_FILE = COMPOSE_DIR / "docker-compose.yml"
SWITCH_SCRIPT = COMPOSE_DIR / "switch_models.sh"

# モデル管理バックエンド ("compose" = 従来 / "modelctl" = WSL の model-ctl.sh)
BACKEND = config.BACKEND


def _is_modelctl() -> bool:
    return BACKEND == "modelctl"


# プロファイル対応表 (profile -> service/container/port) は docker-compose.yml から
# 動的に読み込む(モデル追加に自動追従。_load_profiles 参照)。
# 切替実行のため switch_models.sh 側でも該当 profile が定義されている必要がある。
_profiles_cache: dict[str, dict] = {}
_profiles_mtime: float | None = None


def _unquote(s: str) -> str:
    """YAML スカラーの両端の一致する引用符を除去する。"""
    s = s.strip()
    if len(s) >= 2 and s[0] == s[-1] and s[0] in ("'", '"'):
        return s[1:-1]
    return s


def _load_profiles() -> dict[str, dict]:
    """docker-compose.yml をパースして {profile: {service, container, port}} を構築する。

    profiles と container_name の両方を持つサービス(モデルサービス)のみを対象とする。
    ファイルの mtime が変わったときのみ再パース(2 秒ポーリングでも低コスト)。
    YAML パーサではなく regex で解析するため、<<: *vllm-common のような
    アンカー参照に依存しない(新規依存ゼロ)。

    modelctl バックエンド (WSL) では docker-compose.yml を読まず、
    model-ctl.sh のモード一覧から同一形状の表を返す。
    """
    if _is_modelctl():
        return modelctl.profiles()

    global _profiles_cache, _profiles_mtime
    try:
        mtime = COMPOSE_FILE.stat().st_mtime
    except OSError:
        return {}
    if _profiles_cache and _profiles_mtime == mtime:
        return _profiles_cache

    profiles: dict[str, dict] = {}
    try:
        lines = COMPOSE_FILE.read_text().splitlines()
    except OSError:
        return {}

    in_services = False
    service: str | None = None
    sub_key: str | None = None  # 現在のリストキー (profiles / ports)
    cur_profile: str | None = None
    cur_container: str | None = None
    cur_port: int | None = None

    def flush() -> None:
        nonlocal cur_profile, cur_container, cur_port
        if service and cur_profile and cur_container:
            profiles[cur_profile] = {
                "service": service,
                "container": cur_container,
                "port": cur_port,
            }
        cur_profile = cur_container = None
        cur_port = None

    for ln in lines:
        if not in_services:
            if re.match(r"^services:\s*$", ln):
                in_services = True
            continue
        if re.match(r"^\S", ln):  # services 以外のトップレベルキーでセクション終了
            break
        sm = re.match(r"^  ([A-Za-z0-9_.-]+):\s*$", ln)
        if sm:  # サービス定義 (2スペースインデント)
            flush()
            service = sm.group(1)
            sub_key = None
            continue
        if service is None:
            continue
        km = re.match(r"^    ([A-Za-z0-9_-]+):(.*)$", ln)
        if km:  # サービスキー (4スペースインデント)
            key, rest = km.group(1), km.group(2).strip()
            if key == "container_name":
                cur_container = _unquote(rest) or None
            elif key in ("profiles", "ports"):
                sub_key = key
            else:
                sub_key = None
            continue
        im = re.match(r"^      - (.+)$", ln)
        if im and sub_key:  # リスト項目 (6スペースインデント)
            val = _unquote(im.group(1))
            if sub_key == "profiles" and cur_profile is None:
                cur_profile = val
            elif sub_key == "ports" and cur_port is None:
                try:
                    cur_port = int(val.split(":")[0])  # ホスト側ポート
                except ValueError:
                    pass

    flush()
    _profiles_cache = profiles
    _profiles_mtime = mtime
    return profiles

# 編集を許可する起動引数 (README「パラメータ表示・編集」参照)
EDITABLE_FLAGS = [
    "--max-model-len",
    "--max-num-seqs",
    "--max-num-batched-tokens",
    "--gpu-memory-utilization",
    "--kv-cache-memory-bytes",
    "--temp",
    "--top-p",
    "--top-k",
]

# 現在実行中のバックグラウンドジョブ(切替または再作成)。同時1件。
_job: dict | None = None

# /metrics のトークンカウンタ差分(スループット算出用: 入力/出力分離)
_prev_metrics: dict = {"ts": None, "prompt": None, "generation": None}
# tensorfold / hibana は専用の prev スロットを使う。他エンジン経路 (modelctl の
# llamacpp/exllama など) が呼ぶ _reset_token_state() は "prompt"/"generation" しか
# 初期化しないため、UI が両方の status を交互に引いても差分が消えない。
for _k in ("ts_hb", "prompt_hb", "generation_hb",
           "live_tf", "ts_tf_live",
           "tok_tf_pre", "sec_tf_pre", "tok_tf_gen", "sec_tf_gen",
           "tok_vllm_pre", "sec_vllm_pre", "tok_vllm_gen", "sec_vllm_gen",
           "tok_sg_pre", "sec_sg_pre", "tok_sg_gen", "sec_sg_gen"):
    _prev_metrics.setdefault(_k, None)

# /metrics 本文を取得した時刻。_engine_kind が本文を取りに行くのは status 組み立ての
# 前半で、now = time.time() を取るのはメトリクス生成時 (status が混雑で数秒かかる)。
# カウンタ差分を割る dt に後者を使うと実測窓より dt が長くなり速度が過小になるため、
# 本文と組になるこの時刻を使う (2026-11-25 実測: status 組み立てが 1 s 弱ずれる)。
_metrics_ts: float = 0.0

# hipfire の /metrics はリクエスト単位の値を累計 histogram (sum/count) でしか出さないため、
# 前回ポーリングとの差分を取る状態を保持する (_hipfire_server_metrics が使う)
_hipfire_prev: dict = {}
# Hibana の decode 秒は e2e 総和 − ttft 総和 (p50 近似) なので、実 TTFT のばらつきで分母が
# 縮む窓がある。そこでは直近有効な値を短 TTL で返す (_hibana_metrics が使う)
_hibana_last: dict = {}


def _tps_of(key: str, metric: str, pm: dict, now: float, ts_key: str = "ts") -> float | None:
    """key に対応する系列の差分 tok/s を算出する。

    直前値が無い・系列が今回存在しない場合は None。前回値保持は front 側で行うため
    バックエンドは算出値(0/None を含む)をそのまま返す。
    ts_key は時刻の保存先。エンジン固有のスロットを渡すと、他エンジン経路が
    _reset_token_state() を呼んでも差分が飛ばない (tensorfold / hibana が使う)。
    """
    if _prev_metrics[key] is not None and metric in pm:
        dt = max(now - _prev_metrics[ts_key], 1e-9)
        return max(0.0, (pm[metric] - _prev_metrics[key]) / dt)
    return None


def _reset_token_state() -> None:
    """カウンタ差分を初期化(メトリクス無効/エンジン切替時)。"""
    _prev_metrics["ts"], _prev_metrics["prompt"], _prev_metrics["generation"] = None, None, None


# ---------------------------------------------------------------- スループット算出
# vLLM / SGLang / TensorFold / Hibana のトークンカウンタは **リクエスト完了時にのみ**
# 増加する (vLLM は observe_one_finished_request、TensorFold は本機実測で
# prompt_tokens_total が 4000 tok 生成の最中に終始 0)。それをポーリング間隔 dt で
# 割ると「完了バースト ÷ dt」という過大値になる (2026-11-25 実測: 10,560 tok の
# prefill は 9.38 s かけて完了した = 真値 1,126 T/s なのに、完了窓の dt 0.97 s で
# 10,900 T/s と表示。5,060 tok では 3,626 T/s、2,660 tok では 2,216 T/s。
# 過大倍率 ≒ prefill 所要時間 ÷ dt で、フロントの POLL_MS=2 s なら 11,356 tok 完了時
# にちょうど 5,678 T/s が出る)。
# 対策は「割る量を dt にしない」こと。エンジンが公開する **秒カウンタ**
# (prefill / decode に費やした累計秒) で割れば、窓の切り分けに依存せず、
# どのクライアントがいつ status を引いても同じ真値になる。
# clef の llamacpp:prompt_tokens_total ÷ prompt_seconds_total (1026 行付近) と
# hipfire の histogram 差分と同じ方針の一般化。

_PREFILL_TOK_KEYS = (
    "tensorfold:prompt_tokens_total",
    "vllm:prompt_tokens_total",
    "sglang:prompt_tokens_total",
)
_PREFILL_SEC_KEYS = (
    # 完了済みリクエストの prefill 秒 (トークンカウンタと同じ「完了時に加算」起点なので
    # 分子分母が整合する)。TensorFold の prefill_busy_seconds_total は使わない:
    # idle unload (engine_parked) でリセットされ累計の起点が壊れ、キャッシュ命中分を
    # 含まないので完了時 prompt_tokens_total と割ると過大になる (2026-11-25 実測 4,851 T/s)。
    "tensorfold:request_prefill_seconds_sum",
    "vllm:request_prefill_time_seconds_sum",
    "sglang:request_prefill_time_seconds_sum",
)
# キャッシュ命中した prompt トークンは prefill の作業ではない。分子から引かないと
# 分母 (実 prefill 秒) と整合せず過大になる (本機実測: 10,560 tok 中 8,192 tok が
# キャッシュ命中で、実 prefill 2,368 tok ÷ 2.18 s = 1,088 T/s が真値)。
_PREFILL_SUB_KEYS = ("tensorfold:prompt_tokens_cached_total", "vllm:prompt_tokens_cached_total")
_DECODE_TOK_KEYS = (
    "tensorfold:generation_tokens_total",
    "vllm:generation_tokens_total",
    "sglang:generation_tokens_total",
)
_DECODE_SEC_KEYS = (
    "tensorfold:request_decode_seconds_sum",
    "vllm:request_decode_time_seconds_sum",
    "sglang:request_decode_time_seconds_sum",
)


def _first_key(pm: dict, keys: tuple[str, ...]) -> str | None:
    """pm に実在する最初の系列名 (エンジンごとに系列名が違うため候補を順に見る)。"""
    for k in keys:
        if pm.get(k) is not None:
            return k
    return None


def _tps_by_seconds(
    pm: dict,
    slot: str,
    tok_keys: tuple[str, ...] = _PREFILL_TOK_KEYS,
    sec_keys: tuple[str, ...] = _PREFILL_SEC_KEYS,
    sub_keys: tuple[str, ...] = _PREFILL_SUB_KEYS,
) -> float | None:
    """累計トークン ÷ 累計秒。ポーリング間隔に依存しない prefill/decode 速度。

    分子は sub_keys (キャッシュ命中トークンのように実作業ではない分) を減算する。
    直近窓の差分比 (Δtok / Δsec) を優先し、その窓に増分が無いときはエンジン起動
    からの累計比にフォールバックする。分母が「実際に prefill/decode に費やした秒」
    なので、完了時にしか増えないカウンタでも過大にならない。
    秒カウンタを持たないエンジン (ExLlamaV3 の /health など) は None を返すので、
    呼び出し側は _tps_of にフォールバックする。
    """
    tk, sk = _first_key(pm, tok_keys), _first_key(pm, sec_keys)
    if tk is None or sk is None:
        return None
    tok, sec = pm[tk], pm[sk]
    for k in sub_keys:
        v = pm.get(k)
        if v is not None:
            tok -= v
    pk, sk_ = f"tok_{slot}", f"sec_{slot}"
    prev_tok, prev_sec = _prev_metrics.get(pk), _prev_metrics.get(sk_)
    _prev_metrics[pk], _prev_metrics[sk_] = tok, sec
    if prev_tok is not None and prev_sec is not None:
        d_sec, d_tok = sec - prev_sec, tok - prev_tok
        if d_sec > 0 and d_tok > 0:
            return d_tok / d_sec
    if sec > 0 and tok > 0:
        return tok / sec
    return None


# ---------------------------------------------------------------- 基本ユーティリティ

def _run(cmd: list[str], timeout: float = 10) -> str:
    """コマンドを実行して stdout を返す(失敗時は空文字列)。"""
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
        return r.stdout
    except (subprocess.SubprocessError, OSError):
        return ""


def _http_get(url: str, timeout: float = 5, headers: list[str] | None = None) -> str:
    cmd = ["curl", "--silent", "--fail", "--max-time", str(timeout)]
    if headers:
        cmd += headers
    cmd.append(url)
    try:
        r = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=timeout + 2,
        )
        return r.stdout if r.returncode == 0 else ""
    except (subprocess.SubprocessError, OSError):
        return ""


def _http_ok(url: str, timeout: float = 5) -> bool:
    """HTTP 200 系を返すかで健全性を判定する(本文は空でも可)。"""
    try:
        r = subprocess.run(
            ["curl", "--silent", "--output", "/dev/null", "--max-time", str(timeout), url],
            capture_output=True,
            text=True,
            timeout=timeout + 2,
        )
        return r.returncode == 0
    except (subprocess.SubprocessError, OSError):
        return False


def _tcp_open(port: int, timeout: float = 0.4) -> bool:
    """TCP 接続だけを試みる (HTTP は叩かない)。

    ExLlamaV3 / TabbyAPI は長い prefill 中に HTTP ワーカーが塞がるため、/health が
    数秒で返らない = 落ちた、ではない。listener が受け付けるなら「プロセスは生きていて
    混雑中」と区別できる (2026-10-28: 259K prefill 中に /health が 3 秒で応答せず
    フロントが「API応答なし」になった)。
    """
    try:
        with socket.create_connection(("127.0.0.1", int(port)), timeout=timeout):
            return True
    except (OSError, ValueError):
        return False


# ---------------------------------------------------------------- status 用キャッシュ
# /api/vllm/status は 2 秒間隔でポーリングされる。長文推論中はモデルの HTTP が塞がれて
# 各 curl が max-time まで待ち、直列で 20.5 秒かかった (2026-10-28 実測)。
# 変わらないもの (エンジン種別・モデル名・ログ末尾) をキャッシュし、取得失敗時は前値を
# 返して表示を維持する。
_ENGINE_TTL = 120.0      # 判定成功時のキャッシュ有効期間
_ENGINE_FAIL_TTL = 10.0  # 混雑で判定できなかったときの再試行間隔
_MODEL_NAME_TTL = 30.0
_MODEL_NAME_FAIL_TTL = 10.0  # /v1/models が混雑で返らないときの再試行間隔
_LOGS_TTL = 5.0
_engine_cache: dict = {"base": None, "kind": None, "last": None, "ts": 0.0, "ttl": _ENGINE_TTL}
_model_name_cache: dict = {"base": None, "name": None, "ts": 0.0}
_logs_cache: dict = {"key": None, "lines": [], "ts": 0.0}


def _engine_kind(base: str) -> tuple[str, str]:
    """(エンジン種別, /metrics 本文) を返す。

    種別判定は /metrics 以外に /.well-known/serviceinfo (TabbyAPI) と /v1/models
    (llama.cpp) を叩くので、推論中は 1 回で 8 秒以上失格する。判定結果はコンテナの
    寿命中で変わらないため、成功時だけ 120 秒キャッシュする。

    **判定失敗 (混雑) を 120 秒キャッシュしない**。種別が既知なら /metrics が空でも
    追加プロブ (計 6 秒) を払わず前回種別を返し、10 秒だけ短キャッシュする
    (stale-while-revalidate)。未知のときは "unknown" を返し、get_status 側で
    プロブを払わず metrics={} にする。

    **base (モデルのポート) が変わったら last を破棄する**。/metrics が常に空のエンジン
    (TabbyAPI は /metrics が 404) では last 採用が serviceinfo 判定を永久にスキップし、
    前機種の種別に固定される (2026-11-01: SGLang → TabbyAPI 切替で prefill/decode 消失)。
    """
    global _metrics_ts
    now = time.time()
    c = _engine_cache
    if c["base"] != base:
        # 前機種の種別 (last) を新しい base に継承しない
        c.update({"base": None, "kind": None, "last": None, "ts": 0.0, "ttl": _ENGINE_TTL})
    if c["base"] == base and now - c["ts"] < c["ttl"]:
        # tabby / unknown は /metrics 本文を使わない (ログ解析で取る) ので取りに行かない。
        # 混雑時に毎回 2 秒払わないため (2026-10-28)
        text = "" if c["kind"] in ("tabby", "unknown") else _http_get(f"{base}/metrics", timeout=2)
        if text:
            _metrics_ts = time.time()
        return c["kind"], text
    text = _http_get(f"{base}/metrics", timeout=2)
    if text:
        _metrics_ts = time.time()
    pm = _parse_vllm_metrics(text) if text else {}
    kind = None
    if any(k.startswith("vllm:") for k in pm):
        kind = "vllm"
    elif any(k.startswith("sglang:") for k in pm):
        kind = "sglang"
    elif any(k.startswith("llamacpp:") for k in pm):
        kind = "llamacpp"
    elif any(k.startswith("tensorfold:") for k in pm):
        kind = "tensorfold"
    elif any(k.startswith("hibana_") for k in pm):
        kind = "hibana"
    elif c["last"]:
        # 混雑で /metrics が空 (TabbyAPI は平常時も 404 で空)。種別が既知なら
        # serviceinfo / /v1/models を叩かずに前回種別を返す (10 秒後に再試行)
        c.update({"base": base, "kind": c["last"], "ts": time.time(), "ttl": _ENGINE_FAIL_TTL})
        return c["last"], text
    elif _is_tabbyapi(base):
        kind = "tabby"
    elif _is_llamacpp(base):
        kind = "llamacpp"
    elif _is_tensorfold(base):
        kind = "tensorfold"
    elif _is_hibana(base):
        kind = "hibana"
    if kind:
        # ts は「書き込み時刻」にする。判定自体に 8 秒かかるので、開始時刻を
        # 入れると TTL が実質 2 秒になり毎コール再判定になった (2026-10-28 プロファイル)
        c.update({"base": base, "kind": kind, "last": kind, "ts": time.time(), "ttl": _ENGINE_TTL})
        return kind, text
    c.update({"base": base, "kind": "unknown", "last": c["last"], "ts": time.time(), "ttl": _ENGINE_FAIL_TTL})
    return "unknown", text


def _model_name(base: str, headers: list[str] | None = None) -> str | None:
    """/v1/models からサーブ中モデル名を取る。

    成功時は 30 秒、失敗 (混雑) 時は 10 秒キャッシュする。失敗時にキャッシュしないと
    毎ポーリング 1.5 秒払い続けることになる。返すのは常に「最後に取れた値」。
    """
    c = _model_name_cache
    now = time.time()
    ttl = _MODEL_NAME_TTL if c["name"] else _MODEL_NAME_FAIL_TTL
    if c["base"] == base and now - c["ts"] < ttl:
        return c["name"]
    txt = _http_get(f"{base}/v1/models", timeout=1.5, headers=headers)
    name = None
    if txt:
        mm = re.search(r'"id"\s*:\s*"([^"]+)"', txt)
        if mm:
            name = mm.group(1)
    c.update({"base": base, "name": name or c["name"], "ts": time.time()})
    return c["name"]


def _logs_tail(container: str, n: int = 30) -> list[str]:
    """docker logs の末尾 n 行を 5 秒キャッシュで返す (2 秒ポーリングの重複を省く)。"""
    now = time.time()
    key = f"{container}:{n}"
    if _logs_cache["key"] == key and now - _logs_cache["ts"] < _LOGS_TTL:
        return _logs_cache["lines"]
    out = _run(["docker", "logs", "--tail", str(n), container], timeout=10)
    lines = out.splitlines()[-n:]
    _logs_cache.update({"key": key, "lines": lines, "ts": now})
    return lines


def _fmt_elapsed(seconds: float) -> str:
    s = int(seconds)
    return f"{s // 3600:02d}:{(s % 3600) // 60:02d}:{s % 60:02d}"


def _hipfire_models_info(port: int) -> dict:
    """hipfire の /v1/models (key 不要) から n_ctx / images を読む (Cline 設定補完用)。

    hipfire は compose の command を持たず環境変数 (HIPFIRE_MODEL) で起動するため、
    Magnitude / Strata と同じく API から取る。KV 容量はカード実測で解決される
    （本機 R9700 では max_seq 262144 / card_cap 477153）ので静的値より正確。
    """
    txt = _http_get(f"http://localhost:{port}/v1/models", 5)
    if not txt:
        return {}
    out: dict = {}
    m = re.search(r'"n_ctx"\s*:\s*(\d+)', txt)
    if m:
        out["n_ctx"] = int(m.group(1))
    # architecture.input_modalities に "image" が立っていれば vision 対応
    if re.search(r'"input_modalities"\s*:\s*\[[^\]]*"image"', txt):
        out["images"] = True
    return out


def _clef_models_info(port: int) -> dict:
    """Clef-Flash（decision モデル）の /v1/models から n_ctx / images / decisions を読む。

    llama-server なので形式は hipfire ではなく llama.cpp と同じ（meta.n_ctx と
    architecture.input_modalities / output_modalities を返す）。
    output_modalities が ["decisions"] のとき生成不可＝/v1/systemone 専用。
    """
    txt = _http_get(f"http://localhost:{port}/v1/models", 5)
    if not txt:
        return {}
    out: dict = {}
    m = re.search(r'"n_ctx"\s*:\s*(\d+)', txt)
    if m:
        out["n_ctx"] = int(m.group(1))
    if re.search(r'"input_modalities"\s*:\s*\[[^\]]*"image"', txt):
        out["images"] = True
    if re.search(r'"output_modalities"\s*:\s*\[[^\]]*"decisions"', txt):
        out["decisions"] = True
    return out


def _hipfire_server_metrics(port: int) -> dict:
    """hipfire の /metrics (Prometheus 形式・API key 不要) をマッピングする。

    hipfire は瞬时スループットを持たない。リクエスト単位の値を histogram
    (sum/count) として累計するだけなので、**前回ポーリングとの差分**で
    「直近に完了したリクエストの平均」を出す (front は 2 秒間隔で叩く)。
    差分が取れないとき (初回ポーリング・生成していない間) は累計平均に
    フォールバックするので、Strata の idle 時のように 0 や "-" には落ちない。
    - 出力スループット = hipfire_decode_tokens_per_second  (直近平均 → 累計平均)
    - 入力スループット = hipfire_prefill_tokens_per_second (同上)
    - TTFT / E2E      = hipfire_ttft_milliseconds / hipfire_request_latency_milliseconds
    - KVCache 使用率  = 直近リクエストの (prompt+output) ÷ context_capacity × 100
      (hipfire は KV 使用量を出さないため Strata と同じ定義の推定値)
    - requests_running = hipfire_queue_depth (in-flight + queued。hipfire は両者を
      区別しないので requests_waiting は None のまま = front では "-")
    """
    m = {
        "requests_running": None,
        "requests_waiting": None,
        "kv_cache_usage_pct": None,
        "e2e_latency_s": None,
        "ttft_s": None,
        "prompt_tokens_per_s": None,
        "generation_tokens_per_s": None,
    }
    txt = _http_get(f"http://localhost:{port}/metrics", 5)
    if not txt:
        return m
    pm = _parse_vllm_metrics(txt)
    if not pm:
        return m

    def recent(name: str) -> float | None:
        """<name>_sum / <name>_count の差分平均。count 増が無ければ累計平均。"""
        s, c = pm.get(f"hipfire_{name}_sum"), pm.get(f"hipfire_{name}_count")
        if s is None or c is None:
            return None
        prev = _hipfire_prev.get(name)
        _hipfire_prev[name] = (s, c)
        if prev is not None and c > prev[1]:
            return (s - prev[0]) / (c - prev[1])
        return _hist_avg(pm, f"hipfire_{name}")

    m["generation_tokens_per_s"] = recent("decode_tokens_per_second")
    m["prompt_tokens_per_s"] = recent("prefill_tokens_per_second")
    ttft = recent("ttft_milliseconds")
    e2e = recent("request_latency_milliseconds")
    if ttft is not None:
        m["ttft_s"] = ttft / 1000.0
    if e2e is not None:
        m["e2e_latency_s"] = e2e / 1000.0
    if not pm.get("hipfire_model_loading"):
        m["requests_running"] = pm.get("hipfire_queue_depth")

    cap = pm.get("hipfire_context_capacity_tokens")
    pt, ct = pm.get("hipfire_prompt_tokens_total"), pm.get("hipfire_completion_tokens_total")
    if cap and pt is not None and ct is not None:
        prev = _hipfire_prev.get("tokens")
        _hipfire_prev["tokens"] = (pt, ct)
        # 初回は累計、以降は差分（直近リクエストが KV を占有した割合）
        used = ((pt - prev[0]) + (ct - prev[1])) if prev else (pt + ct)
        if used > 0:
            m["kv_cache_usage_pct"] = min(100.0, used / float(cap) * 100.0)
            _hipfire_prev["kv"] = (time.time(), m["kv_cache_usage_pct"])
        else:
            # 差分 0 = この 2 秒間にリクエストが完了していない。直近値を 5 秒維持し、
            # その後は 0（Strata / llama.cpp の idle 時の「直近値表示」と同じ挙動）
            ts_kv, last_kv = _hipfire_prev.get("kv", (0.0, None))
            if last_kv is not None and time.time() - ts_kv <= 5.0:
                m["kv_cache_usage_pct"] = last_kv
            else:
                m["kv_cache_usage_pct"] = 0.0
    return m


def _parse_vllm_metrics(text: str) -> dict:
    """vLLM の Prometheus 形式メトリクスを dict に変換する。

    vLLM 26.x では全系列がラベル付き(engine=..., model_name=...)のため、
    ラベルを除去して系列名のみで集約する。
    ゲージは同一系列の複数ラベルに依存せず初出を採用(単一エンジン構成)。
    カウンタ(_total 末尾)のみラベル違いの系列を合計する(SGLang の
    prompt_tokens_total / generation_tokens_total は is_streaming=true/false
    で2系列に分かれるため、false 側だけ採用すると差分が常に 0 になる)。
    """
    m: dict[str, float] = {}
    for line in text.splitlines():
        if line.startswith("#") or not line.strip():
            continue
        parts = line.rsplit(" ", 1)
        if len(parts) != 2:
            continue
        head, val = parts
        name = head.split("{", 1)[0]  # ラベルを除去
        try:
            v = float(val)
        except ValueError:
            continue
        if name.endswith("_total"):
            m[name] = m.get(name, 0.0) + v
        elif name not in m:
            m[name] = v
    return m


def _hist_avg(pm: dict, name: str) -> float | None:
    """ヒストグラムの sum/count 平均(片方が欠けていれば None)。"""
    s, c = pm.get(f"{name}_sum"), pm.get(f"{name}_count")
    if s is not None and c:
        return s / c
    return None


def _sglang_metrics(pm: dict) -> dict:
    """SGLang の Prometheus 系列を vLLM と同じ構造にマッピング(2026-08-27)。

    系列名はイメージ lmsysorg/sglang:dev-qwen38-27b-dflash2 内の
    sglang/srt/observability/metrics_collector.py で検証済み
    (--enable-metrics 指定時。README 実装メモ参照)。
    """
    m = {
        "requests_running": pm.get("sglang:num_running_reqs"),
        "requests_waiting": pm.get("sglang:num_queue_reqs"),
        # token_usage は KV キャッシュプール使用率の 0-1 比率
        "kv_cache_usage_pct": (
            pm["sglang:token_usage"] * 100.0 if "sglang:token_usage" in pm else None
        ),
        "e2e_latency_s": _hist_avg(pm, "sglang:e2e_request_latency_seconds"),
        "ttft_s": _hist_avg(pm, "sglang:time_to_first_token_seconds"),
    }
    if "sglang:prompt_tokens_total" in pm or "sglang:generation_tokens_total" in pm:
        # 秒カウンタ (request_prefill/decode_time_seconds_sum) があればそれで割る。
        # 無いビルドは従来の差分/dt にフォールバック (完了バーストで過大になる既知挙動)。
        pre = _tps_by_seconds(pm, "sg_pre", ("sglang:prompt_tokens_total",), _PREFILL_SEC_KEYS)
        gen = _tps_by_seconds(pm, "sg_gen", ("sglang:generation_tokens_total",), _DECODE_SEC_KEYS, ())
        if pre is None and gen is None:
            now = time.time()
            pre = _tps_of("prompt", "sglang:prompt_tokens_total", pm, now)
            gen = _tps_of("generation", "sglang:generation_tokens_total", pm, now)
            _prev_metrics["ts"] = now
            _prev_metrics["prompt"] = pm.get("sglang:prompt_tokens_total", _prev_metrics["prompt"])
            _prev_metrics["generation"] = pm.get("sglang:generation_tokens_total", _prev_metrics["generation"])
        m["prompt_tokens_per_s"] = pre
        m["generation_tokens_per_s"] = gen
    return m


def _sglang_get_load(base: str) -> dict | None:
    """/metrics が無い場合のフォールバック: SGLang の /get_load から
    実行中・待機リクエスト数を取得。これも無い場合は None(llama.cpp 等)。

    応答は dp_rank 毎のリストのため合計する(現在 DP=1 だが将来対応)。
    """
    txt = _http_get(f"{base}/get_load", timeout=3)
    if not txt:
        return None
    try:
        import json

        data = json.loads(txt)
    except ValueError:
        return None
    if isinstance(data, dict):
        data = [data]
    if not isinstance(data, list) or not all(isinstance(e, dict) for e in data):
        return None
    return {
        "requests_running": sum(e.get("num_reqs", 0) for e in data),
        "requests_waiting": sum(e.get("num_waiting_reqs", 0) for e in data),
    }


# ---------------------------------------------------------------- llama.cpp (--metrics)
# llama.cpp ネイティブサーバー (llama-server) の /metrics 対応。
# 系列名は llama-cpp-vulkan-r9700:latest (v0.5.0-dev build 11339) で検証済み。
# スループットはゲージとして直接公開される(カウンター差分不要)。
# KV キャッシュ使用率は /slots から算出 (n_prompt_tokens / n_ctx)。
# E2E / TTFT はこのバージョンでは未公開 (None → フロント "-" 表示)。


def _is_llamacpp(base: str) -> bool:
    """/v1/models の owned_by が "llamacpp" なら llama.cpp ネイティブサーバー。"""
    txt = _http_get(f"{base}/v1/models", timeout=3)
    if not txt:
        return False
    return '"owned_by":"llamacpp"' in txt or '"owned_by": "llamacpp"' in txt


def _llamacpp_slots_kv(base: str) -> float | None:
    """/slots から KV キャッシュ使用率 (%) を算出。"""
    txt = _http_get(f"{base}/slots", timeout=3)
    if not txt:
        return None
    try:
        import json

        slots = json.loads(txt)
    except (ValueError, TypeError):
        return None
    if not isinstance(slots, list) or not slots:
        return None
    total_tokens = 0
    total_ctx = 0
    for s in slots:
        total_tokens += s.get("n_prompt_tokens", 0) + s.get("n_generation_tokens", 0)
        total_ctx += s.get("n_ctx", 0)
    if total_ctx <= 0:
        return None
    return (total_tokens / total_ctx) * 100.0


def _llamacpp_metrics(pm: dict, base: str, cumulative_fallback: bool = False) -> dict:
    """llama.cpp の Prometheus 系列を vLLM と同じ構造にマッピング。

    cumulative_fallback=True (compose 経路の llama.cpp 用): 瞬时ゲージ
    (prompt_tokens_seconds / predicted_tokens_seconds) は生成中しか立たず idle で 0 に
    落ちるため、累計 counter 平均にフォールバックする (clef / hipfire と同じ方針)。
    実測 (2026-11-26・qwen38hermes): llamacpp:tokens_predicted_total 40 ÷
    tokens_predicted_seconds_total 1.77768 = 22.5 T/s で bench_speed の実測と同水準。
    """
    m = {
        "requests_running": pm.get("llamacpp:requests_processing"),
        "requests_waiting": pm.get("llamacpp:requests_deferred"),
        "kv_cache_usage_pct": _llamacpp_slots_kv(base),
        "e2e_latency_s": None,
        "ttft_s": None,
    }
    # スループット: ゲージとして直接公開 (カウンター差分不要)
    if "llamacpp:prompt_tokens_seconds" in pm:
        m["prompt_tokens_per_s"] = pm["llamacpp:prompt_tokens_seconds"]
    if "llamacpp:predicted_tokens_seconds" in pm:
        m["generation_tokens_per_s"] = pm["llamacpp:predicted_tokens_seconds"]
    if cumulative_fallback:
        # llamacpp:prompt_tokens_total はキャッシュ命中を数えない（命中は
        # prompt_tokens_cached_total に別建て。2026-11-26 実測: 9,812 tok 中 9,808 が命中で
        # 差分は 4、cached の差分は 9,808）。vLLM 用の _PREFILL_SUB_KEYS を真似て
        # 分子から命中を引くと負値になる（実測 -93,908 T/s）。
        # prefill 累計は小リクエスト主体だと実測より低く出る（9,812 tok 1 本の差分は 837.2 T/s、
        # 累計は 135.1 → 717.0 T/s へ収束）
        pt, st = pm.get("llamacpp:prompt_tokens_total"), pm.get("llamacpp:prompt_seconds_total")
        if pt and st and not m.get("prompt_tokens_per_s"):
            m["prompt_tokens_per_s"] = pt / st
        gt = pm.get("llamacpp:tokens_predicted_total")
        gs = pm.get("llamacpp:tokens_predicted_seconds_total")
        if gt and gs and not m.get("generation_tokens_per_s"):
            m["generation_tokens_per_s"] = gt / gs
        # MTP / speculative の受理率。llama.cpp はログに accept rate を出さないが
        # /metrics にはある (2026-11-26 実測: draft 45 / accepted 24 = 53.3%)。
        dr = pm.get("llamacpp:spec_decode_num_draft_tokens_total")
        ac = pm.get("llamacpp:spec_decode_num_accepted_tokens_total")
        if dr:
            m["spec_accept_rate_pct"] = (ac or 0.0) / dr * 100.0
    return m


# ---------------------------------------------------------------- Clef-Flash (clef)
# decision モデルは生成しないので predicted 系は常に 0 が正しい値。llama.cpp の
# 瞬时ゲージ (llamacpp:prompt_tokens_seconds) は prefill 中しか立たず idle では 0 に
# 落ちるため、累計 counter から算出した prefill 平均をフォールバックにする
# （hipfire と同じ方針。README「hipfire の prefill/decode 表示」参照）。


def _clef_metrics(pm: dict, base: str) -> dict:
    """Clef-Flash のメトリクス。llamacpp マッピング + prefill 平均のフォールバック。"""
    m = _llamacpp_metrics(pm, base)
    pt = pm.get("llamacpp:prompt_tokens_total")
    st = pm.get("llamacpp:prompt_seconds_total")
    if pt and st:
        avg = pt / st
        if not m.get("prompt_tokens_per_s"):
            m["prompt_tokens_per_s"] = avg
    # decision モデルは出力トークンを持たない（usage.output_tokens は常に 0）
    m["generation_tokens_per_s"] = 0.0
    return m


# ---------------------------------------------------------------- TabbyAPI (ログ解析メトリクス)
# TabbyAPI は /metrics が 404 のため、docker logs のリクエスト毎統計を解析する。
# ログ形式 (common/gen_logging.py、commit de76ff88 で検証済み):
#   開始: #3932 chat/completions (stream): 200,725 prompt tokens · ...
#   完了: #3932 chat/completions (stream): 986 tokens generated at 67.7 T/s ·
#         prompt 200,725 tokens, 99% cached, 1,557 new in 2.20 s (708 T/s) ·
#         first token 2.20 s, total 16.8 s · draft 757/916 accepted (83%)
# 実行中 = 開始ログあり・完了ログなしのリクエスト (max_batch_size 超過分は待機)。
_TABBY_LOG_TAIL = 300
_TABBY_INFLIGHT_MAX_AGE_S = 3600  # 安全策: 1 時間超の残留エントリを破棄 (クラッシュ等)

_tabby_in_flight: dict[int, float] = {}  # req_id -> 初回確認時刻
_tabby_last: dict = {"req_id": None, "stats": None}  # 直近完了リクエスト


def _reset_tabby_state() -> None:
    """ログ追跡状態を初期化(稼働モデルが TabbyAPI でない/エンジン切替時)。"""
    _tabby_in_flight.clear()
    _tabby_last["req_id"], _tabby_last["stats"] = None, None


def _is_tabbyapi(base: str) -> bool:
    """/.well-known/serviceinfo で TabbyAPI かどうかを判定(llama.cpp 等には無い)。"""
    return "TabbyAPI" in _http_get(f"{base}/.well-known/serviceinfo", timeout=3)


_OWNED_BY_TENSORFOLD_RE = re.compile(r'"owned_by"\s*:\s*"tensorfold"')
_OWNED_BY_HIBANA_RE = re.compile(r'"owned_by"\s*:\s*"hibana"')


def _is_tensorfold(base: str) -> bool:
    """TensorFold native かどうかを判定する。

    /metrics が空になるときの種別プロブ。/metrics が返るときは系列名 (tensorfold:) で
    先に判定されるので、ここは /v1/models を 1 回叩くだけ。
    TensorFold は /v1/models の owned_by に "tensorfold" を出す(v1.3.4 実測)。
    """
    return _OWNED_BY_TENSORFOLD_RE.search(_http_get(f"{base}/v1/models", timeout=3)) is not None


def _is_hibana(base: str) -> bool:
    """Hibana (sharar-project/hibana) かどうかを判定する。

    /v1/models の owned_by は "hibana"、/get_server_info は engine: "hibana" を返す(実測)。
    """
    return _OWNED_BY_HIBANA_RE.search(_http_get(f"{base}/v1/models", timeout=3)) is not None


def _tensorfold_metrics(pm: dict) -> dict:
    """TensorFold native の /metrics を vLLM と同じ構造にマッピングする。

    系列名は本機 v1.3.4 実測。`--metrics-open` を付けないと /metrics は空になるため、
    compose/tensorfold-qwen38/serve.sh は既定で付ける。
    kv_cache_usage_ratio はラベル {pool="0"} 付きだが _parse_vllm_metrics がラベルを落として
    集約する。ttft は tensorfold:time_to_first_token_seconds の sum/count (2026-11-25 実測)。

    prompt_tokens_total / generation_tokens_total は **完了したリクエスト分のみ**加算される
    (本機実測: 10,560 tok の prefill 中、9.38 秒間 prompt_tokens_total は 0 のまま。
    filling_prompt_tokens_left だけが 4096 tok チャンクで減っていく)。それをポーリング
    間隔で割ると完了瞬間に 10,900 T/s という過大値になる (真値 1,126 T/s)。
    2026-11-25 の実測に基づく修正:
    - prefill: 完了済み prompt トークン (キャッシュ命中を除く) ÷ 完了済み prefill 秒。
      増分が無い窓 (プレフィル中・アイドル) は同じ起点の累計比を返すので、
      「プレフィル中は 0 が続き、完了瞬間だけ飛び、その後 0 のまま」という挙動が消える
    - decode: ライブ生成は generation_tokens_running の増分 ÷ /metrics 取得窓。
      完了済み分は request_decode_seconds_sum で割る (ポーリング間隔で割らない)
    """
    kv = pm.get("tensorfold:kv_cache_usage_ratio")
    m = {
        "requests_running": pm.get("tensorfold:requests_running"),
        "requests_waiting": pm.get("tensorfold:requests_waiting"),
        "kv_cache_usage_pct": kv * 100.0 if kv is not None else None,
        "e2e_latency_s": _hist_avg(pm, "tensorfold:request_latency_seconds"),
        "ttft_s": _hist_avg(pm, "tensorfold:time_to_first_token_seconds"),
    }
    # prefill: 完了済み prompt トークンからキャッシュ命中を引いた分 ÷ 完了済み prefill 秒。
    # filling_prompt_tokens_left による瞬時値は採らない。prefill_busy_seconds_total が
    # idle unload (engine_parked) でリセットされて窓の分母が壊れる上、filling が
    # 4096 tok チャンク境界でしか減らないため窓が粗い (2026-11-25 実測で 4,851 T/s が出た)。
    # prefill 速度はモデル固有でほぼ一定なので、直近完了リクエストの平均が実用値になる。
    m["prompt_tokens_per_s"] = _tps_by_seconds(
        pm, "tf_pre", ("tensorfold:prompt_tokens_total",),
        ("tensorfold:request_prefill_seconds_sum",), _PREFILL_SUB_KEYS,
    )

    gen, live = pm.get("tensorfold:generation_tokens_total"), pm.get("tensorfold:generation_tokens_running")
    pm2 = dict(pm)
    if gen is not None and live is not None:
        pm2["tensorfold:generation_tokens_total"] = gen + live
    dec = None
    prev_live, prev_ts = _prev_metrics.get("live_tf"), _prev_metrics.get("ts_tf_live")
    ts_now = _metrics_ts or time.time()
    if (live is not None and prev_live is not None and live > prev_live
            and prev_ts is not None and ts_now > prev_ts):
        dec = (live - prev_live) / (ts_now - prev_ts)
    if dec is None:
        dec = _tps_by_seconds(pm2, "tf_gen", _DECODE_TOK_KEYS, _DECODE_SEC_KEYS, ())
    _prev_metrics["live_tf"], _prev_metrics["ts_tf_live"] = live, ts_now
    m["generation_tokens_per_s"] = dec
    return m

# /get_server_info の spec_stats を 15 秒キャッシュで読む (_hibana_metrics が使う)。
# round_ms は decode ラウンド (1 ステップ) 平均 = TPOT 相当で、/metrics の avg_step_ms は
# prefill を含む e2e 平均なので TPOT に使えない (2026-11-25 実測: round_ms 39.4 ms /
# avg_step_ms 308.9 ms、rounds と decode_steps は常に同値)
_HIBANA_GSI_TTL = 15.0
_hibana_gsi: dict = {"ts": 0.0, "spec": {}}


def _hibana_spec_stats(base: str) -> dict:
    """Hibana の /get_server_info から spec_stats を 15 秒キャッシュで返す。

    目的は rounds / round_ms (decode ラウンド平均) のみ。取得失敗時は {} を返す。
    """
    now = time.time()
    if _hibana_gsi["spec"] and now - _hibana_gsi["ts"] < _HIBANA_GSI_TTL:
        return _hibana_gsi["spec"]
    spec: dict = {}
    body = _http_get(f"{base}/get_server_info", timeout=5)
    try:
        data = json.loads(body) if body else None
        if isinstance(data, dict) and isinstance(data.get("spec_stats"), dict):
            spec = data["spec_stats"]
    except ValueError:
        spec = {}
    if spec:
        _hibana_gsi.update({"ts": now, "spec": spec})
    return spec



def _hibana_metrics(pm: dict, base: str = "") -> dict:
    """Hibana の /metrics を vLLM と同じ構造にマッピングする。

    Hibana の /metrics は HELP/TYPE 行の無い素の `name value` 形式で、接頭辞は `hibana_`
    (vLLM / SGLang のコロン区切りと違う)。_parse_vllm_metrics は # 行を捨てるだけなので
    そのまま読める。系列名は本機 d474b74 実測。

    floor_holds は GB10 統合メモリ用の安全指標 (MemAvailable が host floor の半分を
    下回って scheduler が新規受付を保留した回数)。本機は HIBANA_HOST_FLOOR_GIB=16 で
    運用するので、動く=RAG 併走などでメモリが詰まり始めた合図になる。

    スループット: 秒カウンタは存在しない (2026-11-25 実測の全系列は admitted /
    finished / cancelled / retractions / rejected / prefill_chunks / prefill_tokens /
    decode_steps / decode_tokens / steps / avg_step_ms / queued / prefilling / running /
    floor_holds / ttft_p50 / ttft_p95 / ttft_window_count / ttft_window_capacity /
    messages_server_tools_ignored)。prefill_tokens / decode_tokens は **完了時に一括
    加算**される (本機実測: 4,893 tok の prefill 中も 1,557 のまま、完了と同時に 4,978)
    ので、ポーリング間隔で割ると完了バーストになる。秒は合計値から組み立てる:
    - e2e 秒の総和  = steps × avg_step_ms / 1000  (実測 17 × 477.3 ms = 8.11 s、
      実 e2e 合計 7.62 s と 6% 差)
    - ttft 秒の総和 = ttft_p50 × ttft_window_count (近似。2.4118 × 3 = 7.24 s、実 6.94 s)
    - prefill = prefill_tokens ÷ ttft 総和  (4,978 ÷ 7.24 = 688 T/s。実測 622〜764 T/s。
      prefill_tokens はキャッシュ命中を含まない: 4,893 tok 中 1,472 cached のとき +3,421)
    - decode  は /get_server_info の spec_stats を使う。round_ms が decode ラウンド (1
      ステップ) 平均 = TPOT 相当で、/metrics の avg_step_ms は prefill を含む e2e 平均なので
      TPOT に使えない (実測: round_ms 39.4 ms / avg_step_ms 308.9 ms、rounds == decode_steps)。
      decode = decode_tokens ÷ (rounds × round_ms / 1000)  (34 × 39.44 ms = 1.34 s で
      172 tok → 128 T/s。実測 63〜111 T/s)
    - spec_stats が取れないときだけ e2e 総和 − ttft 総和 (p50 近似) にフォールバックする
      (83 ÷ 0.88 = 94 T/s)。ttft 総和は p50 近似なので、その差が e2e 総和の 5% 未満に
      縮むと分母が発散する (実測: 差 0.029 s で 4,444 T/s)。その場合は従来の差分/dt へ
    - prefill は p50 近似なので、キャッシュ命中で速いリクエスト (実測 2,245 T/s) は過小に
      なる。オーダーが合い、0 に落ちず、呼び出し元で一致する値が出ることを優先した
    累計同士の比なのでポーリング間隔と呼び出し元に依存せず、アイドルでも 0 に落ちない。
    """
    m = {
        "requests_running": pm.get("hibana_running"),
        "requests_waiting": pm.get("hibana_queued"),
        # Hibana は KV 使用率を /metrics に出さない (kv_pages と SSD tier は /get_server_info)
        "kv_cache_usage_pct": None,
        "e2e_latency_s": None,
        "ttft_s": pm.get("hibana_ttft_p50"),
        "floor_holds": pm.get("hibana_floor_holds"),
    }
    if "hibana_prefill_tokens" in pm or "hibana_decode_tokens" in pm:
        steps, step_ms = pm.get("hibana_steps"), pm.get("hibana_avg_step_ms")
        ttft, tcnt = pm.get("hibana_ttft_p50"), pm.get("hibana_ttft_window_count")
        e2e_sum = steps * step_ms / 1000.0 if steps and step_ms else None
        ttft_sum = ttft * tcnt if ttft and tcnt else None
        pt, dt = pm.get("hibana_prefill_tokens"), pm.get("hibana_decode_tokens")
        pre = pt / ttft_sum if pt and ttft_sum else None
        dec = None
        # decode 秒の最良材料は /get_server_info の spec_stats。round_ms は decode ラウンド
        # 平均 (TPOT 相当) で、rounds は decode_steps と常に同値 (2026-11-25 実測 34/34)。
        # 実測: rounds 34 × 39.44 ms = 1.34 s、decode_tokens 172 → 128 T/s (実測 63〜111)
        ss = _hibana_spec_stats(base) if base else {}
        rounds, rms = ss.get("rounds"), ss.get("round_ms")
        if dt and rounds and rms and rms > 0:
            dec = dt / (rounds * rms / 1000.0)
        # spec_stats が取れない窓だけ e2e 総和 − ttft 総和 (p50 近似) にフォールバックする。
        # ttft 総和は p50 近似なので、実 TTFT のばらつきが大きいと両者がほぼ等しくなり
        # 分母が発散する (2026-11-25 実測: 差 0.029 s で 4,444 T/s)。
        # 差が e2e 総和の 5% 以上のときだけ採用する。
        if dec is None and dt and e2e_sum and ttft_sum:
            d = e2e_sum - ttft_sum
            if d >= 0.05 * e2e_sum:
                dec = dt / d
        # 有効な窓の値を 5 秒保持する (Strata の _strata_last_metrics と同じ方針)。
        # 分母が縮んだ窓で 0 に落ちて front が古い値を無期限保持するのを防ぐ
        if dec is not None:
            _hibana_last.update({"ts": time.time(), "gen": dec})
        elif _hibana_last.get("gen") is not None and time.time() - _hibana_last.get("ts", 0.0) <= 5.0:
            dec = _hibana_last.get("gen")
        # 組み立て不能な項目 (steps / ttft 未公開、分母が発散) だけ従来の差分/dt に
        # フォールバックする (完了バーストで過大になる既知の挙動)
        now = None
        if pre is None:
            now = time.time()
            pre = _tps_of("prompt_hb", "hibana_prefill_tokens", pm, now, "ts_hb")
            _prev_metrics["ts_hb"] = now
            _prev_metrics["prompt_hb"] = pm.get("hibana_prefill_tokens", _prev_metrics["prompt_hb"])
        if dec is None:
            if now is None:
                now = time.time()
                _prev_metrics["ts_hb"] = now
            dec = _tps_of("generation_hb", "hibana_decode_tokens", pm, now, "ts_hb")
            _prev_metrics["generation_hb"] = pm.get("hibana_decode_tokens", _prev_metrics["generation_hb"])
        m["prompt_tokens_per_s"] = pre
        m["generation_tokens_per_s"] = dec
    return m


_TABBY_ENTRY_RE = re.compile(r"^\d{2}:\d{2}:\d{2}\.\d+ \w+:")
# WSL (modelctl) はログファイルを読む。model-ctl.sh が起こす起動ログ (logs/ctl-*.log) は
# docker logs と同じ "HH:MM:SS.mmm LEVEL:" 接頭辞だが、TabbyAPI 自身の loguru ファイル
# (logs/2026-10-20_08-09-35_588705.log) は "2026-10-20 08:10:43.124 | INFO |" 形式。
# どちらのファイルも読めるように両形式を受理する (本文側の開始/完了正規表現は共通)。
_TABBY_ENTRY_RE_FILE = re.compile(r"^\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}\.\d+ \| \w+")
_TABBY_ENTRY_RE_ANY = re.compile(
    r"^(?:\d{2}:\d{2}:\d{2}\.\d+ \w+:|\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}\.\d+ \| \w+)"
)
_TABBY_START_RE = re.compile(r"#(\d+) \S+(?: \(stream\))?: ([\d,]+) prompt tokens")
_TABBY_DONE_RE = re.compile(
    r"#(\d+) \S+(?: \(stream\))?: ([\d,]+) tokens generated"
    r"(?: at ([\d,.]+) T/s)?"
    r" · prompt ([\d,]+) tokens, (?:\d+% cached|none cached), ([\d,]+) new in ([\d.]+) s"
    r"(?: \(([\d,]+) T/s\))?"
    r" · (?:queued ([\d.]+) s, )?first token ([\d.]+) s, total ([\d.]+) s"
)


def _tabbyapi_parse_logs(
    text: str, entry_re: "re.Pattern[str] | None" = None
) -> list[tuple[str, int, dict | None]]:
    """TabbyAPI のログを (kind, req_id, stats) の列に解析する。

    entry_re 省略時は docker logs 形式 (_TABBY_ENTRY_RE)。WSL のログファイル
    読み取りでは _TABBY_ENTRY_RE_FILE を渡す。
    loguru が長い行を折返す(継続行は先頭が空白)ため、タイムスタンプで始まる行を
    1 エントリとして継続行を結合してから開始/完了にマッチする。
    kind: "start"(stats=None) / "done"(stats=dict)。
    """
    if entry_re is None:
        entry_re = _TABBY_ENTRY_RE
    entries: list[str] = []
    for ln in text.splitlines():
        if entry_re.match(ln) or not entries:
            entries.append(ln)
        else:
            entries[-1] += " " + ln.strip()
    events: list[tuple[str, int, dict | None]] = []
    for e in entries:
        m = _TABBY_DONE_RE.search(e)
        if m:
            events.append(
                (
                    "done",
                    int(m.group(1)),
                    {
                        "gen_tokens": int(m.group(2).replace(",", "")),
                        "gen_tps": float(m.group(3).replace(",", "")) if m.group(3) else None,
                        "prompt_tokens": int(m.group(4).replace(",", "")),
                        "prompt_time": float(m.group(6)),
                        "ttft_s": float(m.group(9)),
                        "total_s": float(m.group(10)),
                    },
                )
            )
            continue
        m = _TABBY_START_RE.search(e)
        if m:
            events.append(("start", int(m.group(1)), None))
    return events


_TABBY_MODEL_CFG_TTL = 120.0
_tabby_model_cfg_cache: dict = {"base": None, "txt": "", "ts": 0.0}


def _tabby_model_txt(base: str, headers: list[str] | None = None) -> str:
    """/v1/model の本文を 120 秒キャッシュで返す。

    使われるのは cache_size / max_batch_size の静的な値だけで、推論中は 3 秒ブロックする
    (2026-10-28: /api/vllm/status が 20.5 秒かかった内 3 秒がここ)。失敗時は前値を返す。
    """
    now = time.time()
    c = _tabby_model_cfg_cache
    if c["base"] == base and now - c["ts"] < _TABBY_MODEL_CFG_TTL:
        return c["txt"]
    txt = _http_get(f"{base}/v1/model", timeout=1.5, headers=headers)
    if txt:
        c.update({"base": base, "txt": txt, "ts": now})
        return txt
    if c["base"] == base:
        return c["txt"]  # 混雑で応答しなくても前値 (静的値なので表示は変わらない)
    return ""


def _tabbyapi_metrics(
    base: str,
    container: str,
    log_text: str | None = None,
    headers: list[str] | None = None,
) -> dict:
    """TabbyAPI メトリクス: ログのリクエスト毎統計を解析する(/metrics なし)。

    log_text 省略時は docker logs を読む(従来動作 = atom1 / atom2)。
    WSL (modelctl) はコンテナを立てないため、呼び出し側がログファイル末尾を
    読み込んで log_text に渡す。headers は WSL で API キーが設定されている場合のみ
    渡す (/v1/model が 401 になると cache_size / max_batch_size が取れない)。

    実行/待機: ログの req_id 追跡(開始あり・完了なし)。max_batch_size 超過分は待機。
    KVCache: (直近リクエストの prompt+gen トークン) / cache_size の推定値
    (TabbyAPI に直接使用率 API は無い。KV キャッシュは直近会話分を保持するため)。
    E2E/TTFT/スループット: 直近完了リクエストの値。前回値保持は front 側で行うため
    ここでは算出値(0/None を含む)をそのまま返す。
    入力スループットは全プロンプトトークン換算(prompt_tokens / prompt_time)で
    vLLM の prompt_tokens_per_s と同一セマンティクス。
    """
    now = time.time()
    if log_text is None:
        events = _tabbyapi_parse_logs(
            _run(["docker", "logs", "--tail", str(_TABBY_LOG_TAIL), container], timeout=10)
        )
    else:
        events = _tabbyapi_parse_logs(log_text, _TABBY_ENTRY_RE_ANY)
    for kind, rid, stats in events:
        if kind == "start":
            _tabby_in_flight.setdefault(rid, now)
        else:
            # 完了は req_id の新旧に関わらず in_flight から除去する
            # (tail 窓内の古い完了も対象。_tabby_last の更新のみ最大 id のみ)
            _tabby_in_flight.pop(rid, None)
            if _tabby_last["req_id"] is None or rid > _tabby_last["req_id"]:
                _tabby_last["req_id"], _tabby_last["stats"] = rid, stats
    for rid in [r for r, ts in _tabby_in_flight.items() if now - ts > _TABBY_INFLIGHT_MAX_AGE_S]:
        _tabby_in_flight.pop(rid, None)

    # cache_size / max_batch_size は /v1/model から (120 秒キャッシュ・静的値)
    cache_size = max_batch_size = None
    model_txt = _tabby_model_txt(base, headers)
    if model_txt:
        cm = re.search(r'"cache_size"\s*:\s*(\d+)', model_txt)
        bm = re.search(r'"max_batch_size"\s*:\s*(\d+)', model_txt)
        if cm:
            cache_size = int(cm.group(1))
        if bm:
            max_batch_size = int(bm.group(1))
    if max_batch_size is None:
        max_batch_size = 1

    n = len(_tabby_in_flight)
    m: dict = {
        "requests_running": min(n, max_batch_size),
        "requests_waiting": max(n - max_batch_size, 0),
    }
    stats = _tabby_last["stats"]
    if stats:
        if cache_size:
            m["kv_cache_usage_pct"] = min(
                100.0, (stats["prompt_tokens"] + stats["gen_tokens"]) / cache_size * 100.0
            )
        m["e2e_latency_s"] = stats["total_s"]
        m["ttft_s"] = stats["ttft_s"]
        m["prompt_tokens_per_s"] = (
            stats["prompt_tokens"] / stats["prompt_time"] if stats["prompt_time"] > 0 else None
        )
        m["generation_tokens_per_s"] = stats["gen_tps"]
    return m


# ================================================================ modelctl (WSL2) メトリクス
# backend=modelctl ではコンテナが立たない。エンジン種別ごとに HTTP / ログファイルから
# vLLM と同じ構造のメトリクスを組み立てる (実装メモ 2026-10-21)。
#
# - Strata           : /metrics/prometheus が llama.cpp と同名系列 (llamacpp:*) を公開する
#                      ため _llamacpp_metrics を流用。加えてログの "[strata] done:" 行から
#                      直近リクエスト・expert cache hit 率・投機受理を補完する。
# - llama.cpp (clef) : --metrics 有効。_clef_metrics が llamacpp: 系列をマッピングする
#                      （decision モデル・生成統計は出ない）。
# - TabbyAPI         : ログファイル末尾を _tabbyapi_metrics に渡す (既存パーサを流用)。

_STRATA_DONE_RE = re.compile(
    r"\[strata\] done: ([\d,]+) tokens in ([\d.]+) s \(([\d.]+) tok/s\)"
    r"(?:.*?expert cache ([\d.]+)% hit)?"
)
_STRATA_PROGRESS_RE = re.compile(
    r"\[strata\] (?:thinking|answering|writing)[^:]*: ([\d,]+) of .*?([\d.]+) tok/s"
)
# llama-server の print_timing (llama.cpp 標準ログ)
_LLAMACPP_LOG_PROMPT_RE = re.compile(
    r"prompt eval time =\s*([\d.]+) ms /\s*([\d,]+) tokens .*?([\d,.]+) tokens per second"
)
_LLAMACPP_LOG_EVAL_RE = re.compile(
    r"eval time =\s*([\d.]+) ms /\s*([\d,]+) tokens .*?([\d,.]+) tokens per second"
)
_LLAMACPP_LOG_TOTAL_RE = re.compile(r"total time =\s*([\d.]+) ms /\s*([\d,]+) tokens")


# strataq3 の /metrics/prometheus には llamacpp: 累计秒カウンタがある。瞬时ゲージ
# (llamacpp:prompt_tokens_seconds) は idle 0 になるため、506fc84 の「割る量を dt に
# しない」方式と同じく累计比でフォールバックする。cached_total は prompt_tokens_total
# より大きく分子から引けないため clef 分岐と同じく減算しない。
_STRATA_PRE_TOK = ("llamacpp:prompt_tokens_total",)
_STRATA_PRE_SEC = ("llamacpp:prompt_seconds_total",)
_STRATA_GEN_TOK = ("llamacpp:tokens_predicted_total",)
_STRATA_GEN_SEC = ("llamacpp:tokens_predicted_seconds_total",)


def _strata_metrics(base: str, log_text: str) -> dict:
    """Strata のメトリクス: /metrics/prometheus (llamacpp: 系列) + ログ補完。"""
    pm = _parse_vllm_metrics(_http_get(f"{base}/metrics/prometheus", timeout=5))
    m = _llamacpp_metrics(pm, base)
    # 506fc84 の「割る量を dt にしない」方式を strataq3 の llamacpp: 累计秒カウンタに適用。
    # 瞬时ゲージが idle 0 の窓は累计比にフォールバックし、どの poll 窓でも同じ値になる。
    if not m.get("prompt_tokens_per_s"):
        m["prompt_tokens_per_s"] = _tps_by_seconds(
            pm, "strata_pre", _STRATA_PRE_TOK, _STRATA_PRE_SEC, ())
    if not m.get("generation_tokens_per_s"):
        m["generation_tokens_per_s"] = _tps_by_seconds(
            pm, "strata_gen", _STRATA_GEN_TOK, _STRATA_GEN_SEC, ())
    running = 0
    for ln in reversed(log_text.splitlines()):
        mp = _STRATA_PROGRESS_RE.search(ln)
        if mp:
            running = 1
            m["generation_tokens_per_s"] = float(mp.group(2).replace(",", ""))
            break
    for ln in reversed(log_text.splitlines()):
        md = _STRATA_DONE_RE.search(ln)
        if md:
            secs = float(md.group(2))
            m["e2e_latency_s"] = secs if secs > 0 else None
            if not m.get("generation_tokens_per_s"):
                m["generation_tokens_per_s"] = float(md.group(3))
            if md.group(4):
                # expert cache hit 率は Strata 独自の会話/重みキャッシュ命中率。
                # KV キャッシュ使用率が取れない場合の代替指標として併記する。
                m["expert_cache_hit_pct"] = float(md.group(4))
            break
    if not m.get("requests_running"):
        m["requests_running"] = running
    if m.get("requests_waiting") is None:
        m["requests_waiting"] = 0
    return m


def _llamacpp_log_metrics(base: str, log_text: str) -> dict:
    """llama.cpp (--metrics 未有効) のログ解析: print_timing から算出する。

    KV キャッシュ使用率は /slots、スループット・E2E・TTFT は直近の
    prompt eval / eval / total 行から取る。
    """
    m = _llamacpp_metrics({}, base)  # /slots による KV + requests 状態
    lines = log_text.splitlines()
    for ln in reversed(lines):
        mp = _LLAMACPP_LOG_PROMPT_RE.search(ln)
        if mp:
            m["prompt_tokens_per_s"] = float(mp.group(3).replace(",", ""))
            m["ttft_s"] = float(mp.group(1)) / 1000.0
            break
    for ln in reversed(lines):
        me = _LLAMACPP_LOG_EVAL_RE.search(ln)
        if me:
            m["generation_tokens_per_s"] = float(me.group(3).replace(",", ""))
            break
    for ln in reversed(lines):
        mt = _LLAMACPP_LOG_TOTAL_RE.search(ln)
        if mt:
            m["e2e_latency_s"] = float(mt.group(1)) / 1000.0
            break
    return m


def _modelctl_headers(profile: str) -> list[str] | None:
    """WSL モードの API キー (model-ctl.sh の _mc_mode_key が正本) を curl 用引数にする。

    鍵が無いモード (strata / clef) と、ログ読みのみのモードでは None = 従来どおり無認証。
    """
    key = (modelctl.params(profile).get("api_key") or "").strip()
    return ["-H", f"Authorization: Bearer {key}"] if key else None


def _modelctl_metrics(profile: str, info: dict, base: str) -> dict:
    """modelctl バックエンドのメトリクス取得をエンジン種別で振り分ける。"""
    engine = info.get("engine", "unknown")
    log_path = info.get("log", "")
    if engine == "tabbyapi":
        _reset_token_state()
        return _tabbyapi_metrics(
            base,
            profile,
            modelctl.read_log_text(log_path, _TABBY_LOG_TAIL),
            _modelctl_headers(profile),
        )
    if engine == "strata":
        _reset_token_state()
        _reset_tabby_state()
        return _strata_metrics(base, modelctl.read_log_text(log_path, 200))
    if engine == "llamacpp":
        _reset_token_state()
        _reset_tabby_state()
        # clef は decision モデル（llama-server --metrics 起動）。ログ解析では
        # 生成統計が出ないため、compose 版 get_status と同じ _clef_metrics
        # （llamacpp マッピング + idle 時 prompt counter 平均フォールバック）を使う
        if profile == "clef":
            mt = _http_get(f"{base}/metrics", timeout=5)
            pm = _parse_vllm_metrics(mt) if mt else {}
            return _clef_metrics(pm, base)
        return _llamacpp_log_metrics(base, modelctl.read_log_text(log_path, 400))


def _get_status_modelctl() -> dict:
    """modelctl バックエンド (WSL) の状態取得。

    compose 版 get_status() と同じ JSON 形状を返す (フロントは無変更で動く)。
    container キーには表示用のモデルラベルを入れる (コンテナ実体は無い)。
    """
    out: dict = {"containers": [], "active": None}
    profiles = _load_profiles()
    running_profile = None
    running_uptime = None
    for profile, info in profiles.items():
        running = bool(info.get("running"))
        if running:
            running_profile = profile
        up = modelctl.uptime_str(profile) if running else None
        if running:
            running_uptime = up
        out["containers"].append(
            {
                "profile": profile,
                "container": info.get("label", profile),
                "running": running,
                "status": "running" if running else "exited",
                "uptime_s": None,
                "uptime_str": up,
                "restart_count": 0,
                "ready": info.get("ready", False),
                "engine": info.get("engine"),
            }
        )

    out["switching"] = _switching_info()

    # 最終稼働プロファイル (フロントのヘッダプルダウンのフォールバック選択用)
    if running_profile is not None:
        config.save_last_active(running_profile)
    out["last_active"] = running_profile or config.load_last_active()

    if running_profile is None:
        return out

    info = profiles[running_profile]
    port = info.get("port")
    base = f"http://localhost:{port}" if port else ""
    active: dict = {
        "profile": running_profile,
        "service": running_profile,
        "container": info.get("label", running_profile),
        "port": port,
        "api_url": base,
        "engine": info.get("engine"),
        "running": True,
        "status": "running",
        "uptime_s": None,
        "uptime_str": running_uptime,
        "restart_count": 0,
        "health": False,
        "model_name": None,
        "metrics": {},
        "logs": [],
    }
    if base:
        hdrs = _modelctl_headers(running_profile)
        # TabbyAPI は /health が無く /v1/models が応答 (model-ctl.sh の待機判定と同じ)。
        # 長文推論中は両方が塞がるので TCP 接続だけで「混雑中 (busy)」を区別する
        active["health"] = _http_ok(f"{base}/health", timeout=1.2) or bool(
            _model_name(base, hdrs)
        )
        active["api_listening"] = True if active["health"] else _tcp_open(port)
        active["busy"] = bool(active["api_listening"] and not active["health"])
        # モデル名は 30 秒キャッシュ (混雑時は前値を維持して表示を止めない)
        active["model_name"] = _model_name(base, hdrs)
        try:
            active["metrics"] = _modelctl_metrics(running_profile, info, base)
        except Exception:
            active["metrics"] = {}
        active["logs"] = modelctl.tail_log(info.get("log", ""), 30)
    else:
        active["logs"] = modelctl.tail_log(info.get("log", ""), 30)

    out["active"] = active
    return out


def _container_state(name: str) -> dict:
    out = _run(["docker", "inspect", name])
    if not out:
        return {}
    try:
        import json

        c = json.loads(out)[0]
        created = c.get("Created", "")
        now = time.time()
        uptime = None
        if c.get("State", {}).get("Running") and created:
            try:
                from datetime import datetime

                t = datetime.fromisoformat(created.replace("Z", "+00:00"))
                uptime = max(0.0, now - t.timestamp())
            except ValueError:
                uptime = None
        return {
            "running": c.get("State", {}).get("Running", False),
            "status": c.get("State", {}).get("Status"),
            "uptime_s": uptime,
            "uptime_str": _fmt_elapsed(uptime) if uptime is not None else None,
            "restart_count": c.get("RestartCount", 0),
        }
    except (ValueError, IndexError, KeyError):
        return {}


def _switching_info() -> dict | None:
    """実行中ジョブがある場合、対象プロファイルと起動準備完了フラグを返す。

    フロントのモデル一覧で「停止中…/切替中…」の状態表示に使う。
    ready は対象ポートの /health が応答するか(switch_models.sh の完了判定と同じ)。
    """
    if _job is None or _job["proc"].poll() is not None:
        return None
    info = _load_profiles().get(_job["profile"])
    ready = False
    if info and info.get("port"):
        ready = _http_ok(f"http://localhost:{info['port']}/health", timeout=3)
    return {"kind": _job["kind"], "profile": _job["profile"], "ready": ready}


def get_status() -> dict:
    """稼働中モデルの状態 + vLLM メトリクスを返す。"""
    if _is_modelctl():
        return _get_status_modelctl()

    out: dict = {"containers": [], "active": None}

    # 各コンテナの稼働状態 (対応表は docker-compose.yml から動的取得)
    profiles = _load_profiles()
    running_profile = None
    for profile, info in profiles.items():
        st = _container_state(info["container"])
        if st.get("running"):
            running_profile = profile
        out["containers"].append({"profile": profile, "container": info["container"], **st})

    # 排他環境 (atom2): RAG embedding の稼働有無をモデル切替警告表示用に付与
    # (遅延 import: rag.py 側も vllm を関数内で参照する循環構成)
    if config.EXCLUSIVE_LLM_RAG:
        try:
            import rag

            out["rag_running"] = rag.embedding_running()
        except Exception:
            out["rag_running"] = False

    # 切替/再作成ジョブ実行中の状態表示用(停止処理中は稼働コンテナが無いため、
    # 早期 return より前に付与する)
    out["switching"] = _switching_info()

    # 最終稼働プロファイル (フロントのヘッダプルダウンのフォールバック選択用。再起動を跨ぐ)
    if running_profile is not None:
        config.save_last_active(running_profile)
    out["last_active"] = running_profile or config.load_last_active()

    if running_profile is None:
        return out

    info = profiles[running_profile]
    port = info["port"]
    base = f"http://localhost:{port}"

    active: dict = {
        "profile": running_profile,
        "service": info["service"],
        "container": info["container"],
        "port": port,
        "api_url": base,
    }
    active.update(_container_state(info["container"]))

    # API 健全性 (vLLM の /health は本文空・HTTP 200 が正常)
    # 長文推論中は ExLlamaV3 の HTTP ワーカーが塞がり /health が 3 秒で返らない。
    # TCP 接続だけを見て「プロセスは生きている＝混雑中 (busy)」を区別する。
    active["health"] = _http_ok(f"{base}/health", timeout=1.2)
    active["api_listening"] = True if active["health"] else _tcp_open(port)
    active["busy"] = bool(active["api_listening"] and not active["health"])

    # サーブ中モデル名。30 秒キャッシュで混雑時は前値を維持する
    # （2026-11-27 のモデル整理で Bearer key 必須の Strata は削除。残存ラインは認証不要）
    active["model_name"] = _model_name(base, None)

    # hipfire: /metrics は Prometheus 形式 (API key 不要)。瞬时スループットを持たないため
    # 累計 histogram (sum/count) の差分で直近平均を出す (_hipfire_server_metrics 参照)。
    # _engine_kind に hipfire 系名 (hipfire_*) の判定は無いため、通すと "unknown" で
    # metrics={} になり prefill/decode が空欄になる。ここで早期 return する
    if running_profile == "hipfire":
        _reset_token_state()
        _reset_tabby_state()
        active["metrics"] = _hipfire_server_metrics(port)
        active["logs"] = _logs_tail(info["container"])
        out["active"] = active
        return out

    # 組み込みメトリクス (/metrics): vLLM は常時公開、SGLang は --enable-metrics 指定時のみ
    # vLLM 26.x の系列名: e2e 相当は request_inference_time_seconds、
    # KV キャッシュは kv_cache_usage_perc。エンジン種別は 120 秒キャッシュなので
    # 混雑時に種別判定のプロブ (serviceinfo / /slots / /v1/models) を払わない
    kind, mt = _engine_kind(base)
    pm = _parse_vllm_metrics(mt) if mt else {}
    if kind == "vllm":
        _reset_tabby_state()
        def g(name: str) -> float | None:
            return pm.get(name)

        def avg_sum_count(s: str, c: str) -> float | None:
            sv, cv = g(s), g(c)
            if sv is not None and cv:
                return sv / cv
            return None

        active["metrics"] = {
            "requests_running": g("vllm:num_requests_running"),
            "requests_waiting": g("vllm:num_requests_waiting"),
            "kv_cache_usage_pct": (
                g("vllm:kv_cache_usage_perc") * 100.0
                if g("vllm:kv_cache_usage_perc") is not None
                else None
            ),
            "e2e_latency_s": avg_sum_count(
                "vllm:request_inference_time_seconds_sum",
                "vllm:request_inference_time_seconds_count",
            ),
            "ttft_s": avg_sum_count(
                "vllm:time_to_first_token_seconds_sum",
                "vllm:time_to_first_token_seconds_count",
            ),
            "num_preemptions": g("vllm:num_preemptions_total"),
        }

        # トークンスループット(入力/出力分離)。系列が存在する場合のみ算出
        # (vLLM、または --enable-metrics 有効の SGLang)。
        # 秒カウンタ (request_prefill/decode_time_seconds_sum) で割るのが正。
        # vLLM のトークンカウンタはリクエスト完了時にのみ増えるため、ポーリング間隔で
        # 割ると完了バースト ÷ dt の過大値になる (TensorFold で 2026-11-25 実測)。
        if "vllm:prompt_tokens_total" in pm or "vllm:generation_tokens_total" in pm:
            pre = _tps_by_seconds(pm, "vllm_pre", ("vllm:prompt_tokens_total",), _PREFILL_SEC_KEYS)
            gen = _tps_by_seconds(pm, "vllm_gen", ("vllm:generation_tokens_total",), _DECODE_SEC_KEYS, ())
            if pre is None and gen is None:
                now = time.time()
                pre = _tps_of("prompt", "vllm:prompt_tokens_total", pm, now)
                gen = _tps_of("generation", "vllm:generation_tokens_total", pm, now)
                _prev_metrics["ts"] = now
                _prev_metrics["prompt"] = pm.get("vllm:prompt_tokens_total", _prev_metrics["prompt"])
                _prev_metrics["generation"] = pm.get("vllm:generation_tokens_total", _prev_metrics["generation"])
            active["metrics"]["prompt_tokens_per_s"] = pre
            active["metrics"]["generation_tokens_per_s"] = gen
    elif kind == "sglang":
        # SGLang (--enable-metrics 有効)。系列名マッピングは _sglang_metrics 参照
        _reset_tabby_state()
        active["metrics"] = _sglang_metrics(pm)
    elif kind == "llamacpp":
        # llama.cpp。--metrics 有効なら pm、無効なら pm={} で /slots のみから取得
        _reset_token_state()
        _reset_tabby_state()
        # clef は decision モデル（生成しない）。idle でも prefill 平均を出す
        if running_profile == "clef":
            active["metrics"] = _clef_metrics(pm, base)
        else:
            # compose 経路の llama.cpp は --metrics を付けた構成。瞬时ゲージは idle で 0 に
            # 落ちるので累計 counter 平均にフォールバックする (2026-11-26・qwen38hermes)
            active["metrics"] = _llamacpp_metrics(pm, base, cumulative_fallback=True)
    elif kind == "tensorfold":
        # TensorFold native (--metrics-open 時の /metrics)。系列マッピングは _tensorfold_metrics 参照
        # 注意: ここでは _reset_token_state() を呼ばない。呼ばれると prev (累計トークン/秒) が
        # 毎回消えて窓差分が永遠に取れなくなる (vLLM 分岐と同じ扱い)
        _reset_tabby_state()
        active["metrics"] = _tensorfold_metrics(pm)
    elif kind == "hibana":
        # Hibana (sharar-project/hibana)。/metrics は HELP/TYPE 無し・hibana_ 接頭辞
        # 同上: prev を保つため _reset_token_state() は呼ばない
        _reset_tabby_state()
        active["metrics"] = _hibana_metrics(pm, base)
    elif kind == "tabby":
        # TabbyAPI: /metrics が 404 → docker logs のリクエスト毎統計を解析。
        # ログ解析なのでモデルの HTTP が塞がっているときも値が取れる (混雑時に強い)
        _reset_token_state()
        active["metrics"] = _tabbyapi_metrics(base, info["container"])
    elif kind == "unknown":
        # 混雑でエンジン種別が判定不能 (プロブを払っても返らない)。これ以上叩かず
        # 表示を止める (種別が既知のときは stale で前の種別が使われる)
        _reset_token_state()
        _reset_tabby_state()
        active["metrics"] = {}
    else:
        # /metrics が無い(SGLang の --enable-metrics 未導入時) →
        # SGLang の /get_load で実行中・待機リクエスト数を取得。これもない場合は None
        _reset_token_state()
        _reset_tabby_state()
        active["metrics"] = _sglang_get_load(base)

    # /get_load 等のフォールバックは None を返す (llama.cpp など)。front は null でも
    # 落ちないが、キー参照を揃えるため dict に正規化する
    if not isinstance(active.get("metrics"), dict):
        active["metrics"] = {}

    # 直近ログ (5 秒キャッシュ)
    active["logs"] = _logs_tail(info["container"])

    out["active"] = active
    return out


def get_log(profile: str, tail: int = 500) -> dict:
    """モデルコンテナの docker logs を tail 行取得する(実行ログダイアログ用)。

    停止済みコンテナでもログファイルが残っている限り取得できる。
    コンテナの stdout/stderr 両方を取得するため _run ではなく直接 subprocess。
    Magnitude はコンテナを立てないため systemd journal (journalctl --user) を読む。
    """
    profiles = _load_profiles()
    if profile not in profiles:
        raise ValueError(f"不明なプロファイル: {profile}")
    p = profiles[profile]
    if _is_modelctl():
        # WSL: 起動ログファイル (logs/ctl-*.log) を読む
        return {"profile": profile, "logs": modelctl.tail_log(p.get("log", ""), tail)}
    cmd = ["docker", "logs", "--tail", str(tail), p["container"]]
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=20)
        text = r.stdout + r.stderr
    except subprocess.TimeoutExpired:
        text = ""
    except subprocess.SubprocessError as e:
        text = f"(ログ取得エラー: {e})"
    lines = text.splitlines()[-tail:]
    return {"profile": profile, "logs": lines}


# ---------------------------------------------------------------- バックグラウンドジョブ

def _start_job(kind: str, profile: str, cmd: list[str]) -> dict:
    """バックグラウンドジョブを開始する(既存ジョブがあれば拒否)。"""
    global _job
    if _job is not None and _job["proc"].poll() is None:
        raise RuntimeError("もう1つの処理(切替/再作成)が実行中です")

    log_file = f"/tmp/dgxutil_{kind}_{int(time.time())}.log"
    lf = open(log_file, "w")
    # compose バックエンドは compose ディレクトリを cwd にする (従来どおり)。
    # modelctl (WSL) はそのパスが存在しないため API ディレクトリを使う。
    cwd = str(COMPOSE_DIR) if COMPOSE_DIR.is_dir() else str(Path(__file__).resolve().parent)
    extra: dict = {}
    if _is_modelctl():
        # 起動したモデルサーバーを API サービスのセッションから切り離す
        # (uvicorn 再起動/停止でモデルプロセスを巻き込まないため)
        extra["start_new_session"] = True
    proc = subprocess.Popen(
        cmd,
        stdout=lf,
        stderr=subprocess.STDOUT,
        stdin=subprocess.DEVNULL,
        cwd=cwd,
        **extra,
    )
    _job = {
        "kind": kind,
        "profile": profile,
        "proc": proc,
        "log_file": log_file,
        "started_at": time.time(),
    }
    return job_status()


def job_status() -> dict:
    """現在実行中のジョブの状態を返す(無ければ running=False)。"""
    if _job is None:
        return {"running": False}

    proc = _job["proc"]
    code = proc.poll()
    tail: list[str] = []
    try:
        with open(_job["log_file"], errors="replace") as f:
            raw = [
                re.sub(r"\x1b\[[0-9;?]*[A-Za-z]|\r", "", ln).rstrip()
                for ln in f.readlines()[-60:]
            ]
        # 空行を詰めて表示する(切替スクリプトの上書き表示用に空行が多い)
        tail = [ln for ln in raw if ln.strip()][-12:]
        # 切替スクリプトの上書き表示フレーム(ヘッダー+ログ4行)はファイル上では
        # 毎秒追記されるため、末尾に複数コピーが残る。最後の === ヘッダー行以降の
        # 1フレーム分のみを保持して、ヘッダーが1行だけになるようにする。
        for i in range(len(tail) - 1, -1, -1):
            if tail[i].startswith("===") and tail[i].endswith("==="):
                tail = tail[i : i + 5]
                break
    except OSError:
        pass

    if code is None:
        return {
            "running": True,
            "kind": _job["kind"],
            "profile": _job["profile"],
            "elapsed_s": round(time.time() - _job["started_at"]),
            "success": None,
            "log_tail": tail,
        }
    return {
        "running": False,
        "kind": _job["kind"],
        "profile": _job["profile"],
        "elapsed_s": round(time.time() - _job["started_at"]),
        "success": code == 0,
        "exit_code": code,
        "log_tail": tail,
    }


def switch_model(profile: str) -> dict:
    """モデル切替をバックグラウンド実行する。

    modelctl (WSL): 確認プロンプトは出さず、model-ctl.sh の _mc_launch を
    そのままジョブとして起動する (稼働中の他モデルは自動で排他停止される)。
    """
    profiles = _load_profiles()
    if _is_modelctl():
        if not profiles:
            raise ValueError(f"{config.MODEL_CTL} からモード一覧を読み込めません")
        if profile not in profiles:
            known = ", ".join(sorted(profiles))
            raise ValueError(f"不明なモード: {profile} (既知: {known})")
        return _start_job("switch", profile, modelctl.switch_cmd(profile))
    if not profiles:
        raise ValueError(f"{COMPOSE_FILE} からプロファイル付きサービスを読み込めません")
    if profile not in profiles:
        known = ", ".join(sorted(profiles))
        raise ValueError(f"不明なプロファイル: {profile} (既知: {known})")
    return _start_job("switch", profile, ["bash", str(SWITCH_SCRIPT), profile])


def stop_model(profile: str) -> dict:
    """稼働中モデルを停止する(バックグラウンド)。"""
    profiles = _load_profiles()
    if _is_modelctl():
        if not profiles:
            raise ValueError(f"{config.MODEL_CTL} からモード一覧を読み込めません")
        if profile not in profiles:
            known = ", ".join(sorted(profiles))
            raise ValueError(f"不明なモード: {profile} (既知: {known})")
        if not profiles[profile].get("running"):
            raise ValueError(f"{profile} は稼働していません")
        return _start_job("stop", profile, modelctl.stop_cmd(profile))
    if not profiles:
        raise ValueError(f"{COMPOSE_FILE} からプロファイル付きサービスを読み込めません")
    if profile not in profiles:
        known = ", ".join(sorted(profiles))
        raise ValueError(f"不明なプロファイル: {profile} (既知: {known})")
    container = profiles[profile]["container"]
    st = _container_state(container)
    if not st.get("running"):
        raise ValueError(f"{profile} は稼働していません")
    return _start_job("stop", profile, ["docker", "stop", "--time", "30", container])


# ---------------------------------------------------------------- パラメータ表示・編集

def _service_command_lines(service: str) -> list[str] | None:
    """compose ファイルから対象サービスの command ブロック(フラグ行)を抽出する。"""
    lines = COMPOSE_FILE.read_text().splitlines()
    block: list[str] = []
    in_service = False
    in_command = False
    indent = 0
    for ln in lines:
        if re.match(rf"^  {re.escape(service)}:\s*$", ln):
            in_service = True
            continue
        if in_service:
            if re.match(r"^  \S", ln):  # 次のサービス(2スペースインデント)で終了
                break
            if re.match(r"^    command:\s*>", ln):
                in_command = True
                indent = len(ln) - len(ln.lstrip())
                continue
            m = re.match(r"^    command:\s*(\S.*?)\s*$", ln)  # 単一行 command (TabbyAPI 系)
            if m and not in_command and not block:
                toks = m.group(1).split()
                out: list[str] = [toks[0]]  # プログラム名 (main.py)
                i = 1
                while i < len(toks):
                    if toks[i].startswith("-"):
                        nxt = toks[i + 1] if i + 1 < len(toks) and not toks[i + 1].startswith("-") else None
                        out.append(toks[i] + (f" {nxt}" if nxt else ""))
                        i += 2 if nxt else 1
                    else:
                        i += 1
                return out
            if in_command:
                if ln.strip() == "" or ln.lstrip().startswith("#"):
                    continue
                cur_indent = len(ln) - len(ln.lstrip())
                if cur_indent <= indent:
                    in_command = False
                    continue
                block.append(ln)
    return block if in_command or block else None


def get_params(profile: str) -> dict:
    """稼働モデルのパラメータ(起動引数)を表示する。"""
    profiles = _load_profiles()
    if profile not in profiles:
        raise ValueError(f"不明なプロファイル: {profile}")
    if _is_modelctl():
        # WSL: 設定形式がエンジンごとに異なる (TabbyAPI yml / Strata JSON / kit .env /
        # llama.cpp 起動引数) ため、model-ctl.sh の既存アクセサが読む値を表示専用で返す。
        kv = modelctl.params(profile)
        labels = {
            "mode": "モード",
            "engine": "エンジン",
            "label": "モデル",
            "port": "API ポート",
            "dir": "作業ディレクトリ",
            "start_script": "起動スクリプト",
            "weights_ready": "重み導入済み",
            "running": "稼働中",
            "context_length": "コンテキスト長",
            "max_output_tokens": "最大出力トークン",
            "config_json": "設定 JSON",
            "model_name": "モデル ID",
        }
        params = [
            {"flag": labels.get(k, k), "value": v, "editable": False}
            for k, v in kv.items()
        ]
        return {"profile": profile, "service": profile, "params": params}
    service = profiles[profile]["service"]
    block = _service_command_lines(service)
    if block is None:
        raise ValueError(f"compose にサービス {service} の command が見つかりません")

    params = []
    for ln in block:
        parts = ln.strip().split(None, 1)
        flag = parts[0]
        value = parts[1] if len(parts) > 1 else None
        params.append(
            {
                "flag": flag,
                "value": value,
                "editable": flag in EDITABLE_FLAGS,
            }
        )
    return {"profile": profile, "service": service, "params": params}


def _set_param_in_compose(service: str, flag: str, value: str) -> None:
    """compose ファイル内の command ブロックから flag の値を書き換える。"""
    lines = COMPOSE_FILE.read_text().splitlines(keepends=True)
    in_service = False
    in_command = False
    flag_re = re.compile(rf"^( *){re.escape(flag)}(\s+)(.*)$")

    for i, ln in enumerate(lines):
        stripped = ln.rstrip("\n")
        if re.match(rf"^  {re.escape(service)}:\s*$", stripped):
            in_service = True
            continue
        if in_service:
            if re.match(r"^  \S", stripped):
                break
            m = re.match(r"^(\s*)command:\s*>", stripped)
            if m:
                in_command = True
                continue
            if in_command:
                fm = flag_re.match(stripped)
                if fm:
                    lines[i] = f"{fm.group(1)}{flag} {value}\n"

    if not in_service:
        raise ValueError(f"compose にサービス {service} が見つかりません")
    COMPOSE_FILE.write_text("".join(lines))


def edit_params(profile: str, updates: dict[str, str]) -> dict:
    """パラメータを編集し、コンテナを再作成する(バックグラウンド)。

    vLLM / llama.cpp のパラメータは起動時に確定するため、
    compose の command を書き換えた後に --force-recreate する。
    """
    if _is_modelctl():
        # WSL: compose を使わないため編集は未対応 (表示のみ)。フロントも
        # /api/platform の params_editable=false で保存ボタンを非表示にする。
        raise ValueError("modelctl バックエンドではパラメータ編集はできません (表示のみ)")
    profiles = _load_profiles()
    if profile not in profiles:
        raise ValueError(f"不明なプロファイル: {profile}")
    service = profiles[profile]["service"]

    for flag, value in updates.items():
        if flag not in EDITABLE_FLAGS:
            raise ValueError(f"編集対象外の引数: {flag}")
        _set_param_in_compose(service, flag, str(value))

    cmd = [
        "docker", "compose",
        "--profile", profile,
        "up", "-d", "--force-recreate",
        service,
    ]
    return _start_job("recreate", profile, cmd)


# ---------------------------------------------------------------- Cline 設定値

# Cline 設定の推奨値テーブル (プロファイル別)。
# compose の command に無い値(コンテキスト最大・最大出力トークン・温度・画像対応)を
# ここで補完する。画像対応・温度は compose の command から動的取得できる場合は
# それを優先し、取得できない場合のみこのテーブルを使う。
CLINE_MODEL_TABLE = {
    "nemotron": {
        "images": False,
        "context_max": 262144,
        "max_output_recommended": 8192,
        "max_output_max": 32768,
        "temperature": 0.6,
    },
    "muse": {
        "images": True,  # 動的: --mmproj
        "context_max": 131072,
        "max_output_recommended": 8192,
        "max_output_max": 32768,
        "temperature": 1.0,  # 動的: --temp
    },
    "qwen38": {
        "images": False,
        "context_max": 262144,
        "max_output_recommended": 8192,
        "max_output_max": 32768,
        "temperature": 0.6,
    },
    "qwen38bf16": {
        "images": True,  # 動的: --limit-mm-per-prompt
        "context_max": 262144,
        "max_output_recommended": 8192,
        "max_output_max": 32768,
        "temperature": 0.6,
    },
    "qwen38nvfp4": {
        "images": False,
        "context_max": 262144,
        "max_output_recommended": 8192,
        "max_output_max": 32768,
        "temperature": 0.6,
    },
    "qwen38sglang": {
        "images": False,
        "context_max": 262144,
        "max_output_recommended": 8192,
        "max_output_max": 32768,
        "temperature": 0.6,
    },
    "qwen38sglangeagle": {
        "images": False,
        "context_max": 262144,
        "max_output_recommended": 8192,
        "max_output_max": 32768,
        "temperature": 0.6,
    },
    "qwen38sglangdflash": {
        "images": False,
        "context_max": 1000000,
        "max_output_recommended": 8192,
        "max_output_max": 32768,
        "temperature": 0.6,
    },
    "qwen38sglangdspark": {
        "images": False,
        "context_max": 1000000,
        "max_output_recommended": 8192,
        "max_output_max": 32768,
        "temperature": 0.6,
    },
    "qwen38flashnextexl3": {
        "images": True,
        "context_max": 524288,
        "max_output_recommended": 8192,
        "max_output_max": 32768,
        "temperature": 0.6,
    },
    "qwen38flashnextexl3_3p05": {
        "images": True,
        "context_max": 524288,
        "max_output_recommended": 8192,
        "max_output_max": 32768,
        "temperature": 0.6,
    },
    "qwen38flashnextexl3_4p05": {
        "images": True,  # vision: true（vision_k6.safetensors・6bit 量子化）
        "context_max": 1048576,  # YaRN factor 4.0（cache_size 1048576・Q8）
        "max_output_recommended": 8192,
        "max_output_max": 32768,
        "temperature": 0.6,
    },
    "mimo_flash": {
        "images": False,  # テキスト専用（Vision/Audio なし）
        "context_max": 262144,  # KV cache_size（1M Q8 は MemAvailable 不足のため 262K）
        "max_output_recommended": 8192,
        "max_output_max": 32768,
        "temperature": 0.6,
    },
    # --- WhitebearATOM2 (R9700 / VRAM 32GB) 用 -----------------
    # atom2 の strata / qwen38radiance / qwen38sglangrocm / qwen38gguf は 2026-11-27 の
    # モデル整理で compose から削除した。ただしこの 3 キーは他機（atom1 / WSL modelctl）の
    # 同名プロファイル用に残っているため、共有の静的フォールバック表からは外さない
    # （qwen38gguf は 2026-11-27 の a3573b4 で他機からも削除済み）。
    "strata": {
        "images": True,  # 動的: /health の images（table はフォールバック）
        "context_max": 131072,  # 動的: /health の max_context（IQ3_XXS 既定 128K）
        "max_output_recommended": 8192,
        "max_output_max": 32768,
        "temperature": 0.6,
    },
    "qwen38radiance": {
        "images": True,  # 動的: --limit-mm-per-prompt
        "context_max": 262144,
        "max_output_recommended": 8192,
        "max_output_max": 32768,
        "temperature": 0.6,
    },
    "qwen38sglangrocm": {
        "images": False,
        "context_max": 262144,
        "max_output_recommended": 8192,
        "max_output_max": 32768,
        "temperature": 0.6,
    },
    "hipfire": {
        "images": True,  # 動的: /v1/models の input_modalities（2026-11-07 検証済み）
        "context_max": 262144,  # 動的: /v1/models の meta.n_ctx（MQ4V2 のネイティブ 262K）
        # hipfire の KV 予算は prefill + max_tokens + 1 <= 262144（超過は 400 で縮小しない）。
        # 推奨 8192 を保つこと。contextWindow と同じ大きさの max_tokens を送ると
        # prefill が約 16.5K を超えた時点で必ず拒否される（2026-11-07 実測）
        "max_output_recommended": 8192,
        "max_output_max": 32768,
        "temperature": 1.0,  # registry の recommended_settings（top_p 0.95 / top_k 20）
    },
    # --- Clef-Flash (clef・atom2 / 2026-11-21 追加) ---------------------------
    # **decision モデル（生成しない）**。/v1/chat/completions は仕様上 501 を返すため、
    # Cline の chat 経路では使えない（get_cline_config が decision_only=true を返す）。
    # context は /v1/models の n_ctx (262,144) ではなく **--ubatch-size が実効上限**:
    # プロンプト全体を 1 バッチで評価する必要があるためで、実測 15,142 tok 成功 /
    # 18,142 tok で 500（"increase the physical batch size (current batch size: 16384)"）。
    # 公式 transformers レシピの encode_record(max_length=16384) と同じ値。
    # 生成しないので max_output は 0、temperature も無意味（0.0 を返す）
    "clef": {
        "images": True,  # 動的: --mmproj / /v1/models の input_modalities
        "context_max": 16384,  # 動的: compose の -ub（--ctx-size 262144 は実効外）
        "max_output_recommended": 0,
        "max_output_max": 0,
        "temperature": 0.0,
    },
    "flashnexthibana": {
        # Hibana の Flash-Next は vision tower を読み込まない（/get_server_info の
        # vision: false を実測）。thinking は常時 on で reasoning_content に分離される
        # 最大出力トークン（2026-11-25 実測）: 出力単独の固定上限は無く、
        # hibana/engine/executor.py:275 が `prompt + max_tokens > max_context` を弾くので
        # **上限 = 262,144 - prompt_tokens**。最小プロンプト（54 tok）で max_tokens
        # 262,090 受理 / 262,091 で 400 "prompt plus output budget exceeds context limit
        # 262144" を実測。262,144 は config.json の max_position_embeddings（native）で
        # launch/settings.py:221 の DEFAULT_CONTEXT が上限（YaRN 不可）。KV プール
        # 1,157,096 tok（FP8）は 262,144 より大きく非律速。
        # max_output_max 32768 は**実用上限**（技術的上限 262,090 の 1/8）。max_tokens は
        # 思考 + 本文の合計で、2048 は 17/20・8192 で 20/20（compose/ja20_gate.py 実測）
        # なので recommended は 8192 が下限。
        "images": False,
        "context_max": 262144,
        "max_output_recommended": 8192,
        "max_output_max": 32768,
        "temperature": 1.0,
    },
    # --- Qwen 3.8 27B hermes 専用（2026-11-26 追加）---------------------------
    # harness-hub/Qwen3.8-27B-hermes-adapted（LoRA r=32・alpha=64・q/k/v/o + gate/up/down）を
    # llama.cpp の --lora で実行時に適用するアダプタ。基底は Qwen/Qwen3.8-27B で本機と同系譜。
    # アダプタは **thinking OFF** で訓練されているためサーバーは --reasoning off
    # （このビルドでは --chat-template-kwargs '{"enable_thinking": false}' は deprecated 警告）。
    # 実測: thinking ON のまま max_tokens 48 で 48 tok 全部が reasoning_content に入り
    # content が空になった（2026-11-26）。
    # LoRA の適用確認は `-lv 5` のログと logprobs 差分で行うこと。既定 verbosity 3 では
    # llama_adapter_lora_init の行が 1 行も出ず、未適用と誤判定した（2026-11-26 の実測メモ）。
    # context は YaRN 4 で 1,048,576（native 262,144）・単一 stream（--parallel 1）。
    # max_output 推奨 8192 は作者評価が「短い出力」で最良（turn 数 40% 未満・生成トークン約 1/2）
    # なのに合わせ、Cline のエージェント運用の実用値も同じ 8192。
    "qwen38hermes": {
        "images": True,  # 動的: --mmproj
        "context_max": 1048576,
        "max_output_recommended": 8192,
        "max_output_max": 32768,
        "temperature": 0.7,
    },
}


def _command_flag_value(service: str, flags: list[str]) -> str | None:
    """compose の command ブロックから該当フラグの値を返す(無ければ None)。"""
    block = _service_command_lines(service)
    if block is None:
        return None
    for ln in block:
        parts = ln.strip().split(None, 1)
        if parts[0] in flags and len(parts) > 1:
            return parts[1]
    return None


def _command_has_flag(service: str, flags: list[str]) -> bool:
    """compose の command ブロックに該当フラグのいずれかが含まれるか。"""
    block = _service_command_lines(service)
    if block is None:
        return False
    for ln in block:
        if ln.strip().split(None, 1)[0] in flags:
            return True
    return False


def _cline_config_modelctl(active: dict) -> dict:
    """modelctl (WSL) 版の Cline 設定値。

    base URL は WSL 内の実情に合わせて LAN IP を使う (mDNS .local は WSL 内では
    解決できない — model-ctl.sh の案内と同じ)。API キー・コンテキスト長は
    model-ctl.sh の既存アクセサ (_mc_mode_key / _mc_mode_ctx_conf) が読んだ値を正本とする。
    """
    profile = active["profile"]
    port = active["port"]
    kv = modelctl.params(profile)
    host = kv.get("lan_ip") or socket.gethostname()
    model_id = active.get("model_name") or kv.get("model_name") or profile
    key = (kv.get("api_key") or "").strip()
    if key:
        api_key = key
    else:
        api_key = "認証なし(任意の値でOK)"
    ctx_raw = (kv.get("context_length") or "").strip()
    try:
        ctx = int(ctx_raw)
    except ValueError:
        ctx = None
    mo_raw = (kv.get("max_output_tokens") or "").strip()
    try:
        max_out = int(mo_raw)
    except ValueError:
        max_out = None
    table = CLINE_MODEL_TABLE.get(profile, {})
    # Clef-Flash (clef・decision モデル): /v1/chat/completions は 501（生成しない）。
    # compose 分岐と同じく /v1/models の output_modalities=["decisions"] で判別し
    # decision_only を返す（front が注記を表示）。context は model-ctl.sh 側が
    # kit/.env の UB（16,384＝実効入力上限）を返すため、追加パースは不要。
    decision_only = False
    reasoning_options = ["None", "Low", "Medium", "High", "XHigh"]
    if profile == "clef":
        decision_only = bool(_clef_models_info(port).get("decisions"))
        reasoning_options = ["None"]
    return {
        "api_provider": "OpenAI Compatible",
        "base_url": f"http://{host}:{port}/v1/" if port else None,
        "api_key": api_key,
        "model_id": model_id,
        "supports_images": bool(table.get("images", False)),
        "context_recommended": ctx or table.get("context_max"),
        "context_max": table.get("context_max", ctx),
        "max_output_recommended": max_out or table.get("max_output_recommended", 8192),
        "max_output_max": table.get("max_output_max", 32768),
        "temperature": table.get("temperature", 0.6),
        "reasoning_effort_options": reasoning_options,
        "decision_only": decision_only,
    }


def get_cline_config() -> dict:
    """稼働中モデルの Cline 設定値を返す。

    動的取得: モデル名(/v1/models)・ポート・hostname・コンテキスト(compose の
    --max-model-len/--context-length/--ctx-size)・温度(--temp)・画像対応(フラグ)。
    静的補完: compose に無い値は CLINE_MODEL_TABLE から。
    """
    status = get_status()
    active = status.get("active")
    if not active:
        raise ValueError("稼働中のモデルがありません")
    profile = active["profile"]
    service = active["service"]
    port = active["port"]

    if _is_modelctl():
        return _cline_config_modelctl(active)

    # 動的: モデル名・ポート・hostname
    model_id = active.get("model_name") or profile
    hostname = socket.gethostname()

    # 残存ライン（hipfire / qwen3vl / clef）は base path が v1 で API key 不要
    # （2026-11-27 のモデル整理で Magnitude（inference/v1 + API key）と Strata（Bearer key）は削除）
    base_url = f"http://{hostname}.local:{port}/v1/"
    api_key = "認証なし(任意の値でOK)"

    # 動的: コンテキストウィンドウ(推奨)
    ctx = _command_flag_value(service, ["--max-model-len", "--context-length", "--ctx-size"])
    context_recommended = None
    if ctx:
        try:
            context_recommended = int(ctx)
        except ValueError:
            pass

    # 動的: 温度 (--temp は llama.cpp のみ)
    temp = _command_flag_value(service, ["--temp"])
    temperature = None
    if temp:
        try:
            temperature = float(temp)
        except ValueError:
            pass

    # 動的: 画像対応 (vLLM / llama.cpp のフラグ)
    images = _command_has_flag(
        service,
        ["--limit-mm-per-prompt", "--mm-processor-kwargs", "--mmproj"],
    )

    # 静的テーブル補完
    table = CLINE_MODEL_TABLE.get(profile, {})
    if not images:
        images = table.get("images", False)
    context_max = table.get("context_max", context_recommended)
    if context_recommended is None:
        context_recommended = context_max
    # hipfire は compose の command が無く環境変数設定 → /v1/models から動的取得
    # （取れないときのみ CLINE_MODEL_TABLE の 262144 にフォールバックする）
    if profile == "hipfire":
        hi = _hipfire_models_info(port)
        if hi.get("n_ctx"):
            context_recommended = hi["n_ctx"]
            context_max = hi["n_ctx"]
        if "images" in hi:
            images = hi["images"]
    # Clef-Flash は decision モデル（生成しない）。compose の --ctx-size 262144 は張れるが、
    # プロンプト全体を 1 バッチで評価するため **実効上限は --ubatch-size**。
    # /v1/chat/completions は 501 なので decision_only を返し、front に注記を出す
    decision_only = False
    reasoning_options = ["None", "Low", "Medium", "High", "XHigh"]
    if profile == "clef":
        ci = _clef_models_info(port)
        if "images" in ci:
            images = ci["images"]
        decision_only = bool(ci.get("decisions"))
        ub = _command_flag_value(service, ["-ub", "--ubatch-size"])
        if ub and ub.isdigit():
            context_recommended = int(ub)
            context_max = int(ub)
        # 生成しないので reasoning 指定は存在しない（出力トークンは table の 0）
        reasoning_options = ["None"]
    max_output_recommended = table.get("max_output_recommended", 8192)
    max_output_max = table.get("max_output_max", 32768)
    if temperature is None:
        temperature = table.get("temperature", 0.6)

    return {
        "api_provider": "OpenAI Compatible",
        "base_url": base_url,
        "api_key": api_key,
        "model_id": model_id,
        "supports_images": images,
        "context_recommended": context_recommended,
        "context_max": context_max,
        "max_output_recommended": max_output_recommended,
        "max_output_max": max_output_max,
        "temperature": temperature,
        "reasoning_effort_options": reasoning_options,
        # decision モデル（clef）は /v1/systemone 専用で chat/completions が 501。
        # front は true のとき「Cline の chat には使えない」注記を出す
        "decision_only": decision_only,
    }
