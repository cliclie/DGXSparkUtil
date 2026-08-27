"""vLLM 状態モニタリング / モデル切替 / パラメータ編集。

- 状態: docker ps/inspect + vLLM /health, /v1/models, /metrics + docker logs
- プロファイル対応表: docker-compose.yml を動的にパース(モデル追加に自動追従)
- 切替: 既存スクリプト switch_models.sh をバックグラウンド実行
- パラメータ編集: docker-compose.yml の command を書き換えて --force-recreate
"""

from __future__ import annotations

import re
import subprocess
import time
from pathlib import Path

COMPOSE_DIR = Path("/home/cliclie/llm/compose")
COMPOSE_FILE = COMPOSE_DIR / "docker-compose.yml"
SWITCH_SCRIPT = COMPOSE_DIR / "switch_models.sh"

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
    """
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

# /metrics のトークンカウンタ差分(スループット算出用)
_prev_metrics: dict = {"ts": None, "tokens": None}

# 直近の非ゼロスループットを保持。SGLang/vLLM のトークンカウンタはリクエスト完了時のみ
#増加するため、完了が無い間差分が 0 になる → 運用要求により前回値を維持表示する。
_last_tokens_per_s: float | None = None


def _apply_tps(tokens_per_s: float | None) -> float | None:
    """算出スループットが非ゼロなら保持値を更新し、それ以外は保持値を返す。"""
    global _last_tokens_per_s
    if tokens_per_s and tokens_per_s > 0:
        _last_tokens_per_s = tokens_per_s
    return tokens_per_s or _last_tokens_per_s


def _reset_token_state() -> None:
    """カウンタ差分・保持スループットを初期化(メトリクス無効/エンジン切替時)。"""
    global _last_tokens_per_s
    _prev_metrics["ts"], _prev_metrics["tokens"] = None, None
    _last_tokens_per_s = None


# ---------------------------------------------------------------- 基本ユーティリティ

def _run(cmd: list[str], timeout: float = 10) -> str:
    """コマンドを実行して stdout を返す(失敗時は空文字列)。"""
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
        return r.stdout
    except (subprocess.SubprocessError, OSError):
        return ""


def _http_get(url: str, timeout: float = 5) -> str:
    try:
        r = subprocess.run(
            ["curl", "--silent", "--fail", "--max-time", str(timeout), url],
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


def _fmt_elapsed(seconds: float) -> str:
    s = int(seconds)
    return f"{s // 3600:02d}:{(s % 3600) // 60:02d}:{s % 60:02d}"


# ---------------------------------------------------------------- 状態

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
        tokens = (
            pm.get("sglang:prompt_tokens_total", 0.0)
            + pm.get("sglang:generation_tokens_total", 0.0)
        )
        now = time.time()
        if _prev_metrics["tokens"] is not None:
            dt = max(now - _prev_metrics["ts"], 1e-9)
            m["tokens_per_s"] = _apply_tps(max(0.0, (tokens - _prev_metrics["tokens"]) / dt))
        _prev_metrics["ts"], _prev_metrics["tokens"] = now, tokens
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


def _container_state(name: str) -> dict:
    """docker inspect でコンテナ状態を取得する(存在しなければ空 dict)。"""
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

                t = datetime.fromisoformat(created.split("+")[0].replace("Z", ""))
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
    out: dict = {"containers": [], "active": None}

    # 各コンテナの稼働状態 (対応表は docker-compose.yml から動的取得)
    profiles = _load_profiles()
    running_profile = None
    for profile, info in profiles.items():
        st = _container_state(info["container"])
        if st.get("running"):
            running_profile = profile
        out["containers"].append({"profile": profile, "container": info["container"], **st})

    # 切替/再作成ジョブ実行中の状態表示用(停止処理中は稼働コンテナが無いため、
    # 早期 return より前に付与する)
    out["switching"] = _switching_info()

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
    active["health"] = _http_ok(f"{base}/health", timeout=3)

    # サーブ中モデル名
    models = _http_get(f"{base}/v1/models", timeout=3)
    active["model_name"] = None
    if models:
        mm = re.search(r'"id"\s*:\s*"([^"]+)"', models)
        if mm:
            active["model_name"] = mm.group(1)

    # 組み込みメトリクス (/metrics): vLLM は常時公開、SGLang は --enable-metrics 指定時のみ
    # vLLM 26.x の系列名: e2e 相当は request_inference_time_seconds、
    # KV キャッシュは kv_cache_usage_perc
    mt = _http_get(f"{base}/metrics", timeout=5)
    pm = _parse_vllm_metrics(mt) if mt else {}
    if any(k.startswith("vllm:") for k in pm):
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

        # トークンスループット(カウンター差分)。系列が存在する場合のみ算出
        # (vLLM、または --enable-metrics 有効の SGLang)
        if "vllm:prompt_tokens_total" in pm or "vllm:generation_tokens_total" in pm:
            tokens = (
                pm.get("vllm:prompt_tokens_total", 0.0)
                + pm.get("vllm:generation_tokens_total", 0.0)
            )
            now = time.time()
            if _prev_metrics["tokens"] is not None:
                dt = max(now - _prev_metrics["ts"], 1e-9)
                active["metrics"]["tokens_per_s"] = _apply_tps(max(0.0, (tokens - _prev_metrics["tokens"]) / dt))
            _prev_metrics["ts"], _prev_metrics["tokens"] = now, tokens
    elif any(k.startswith("sglang:") for k in pm):
        # SGLang (--enable-metrics 有効)。系列名マッピングは _sglang_metrics 参照
        active["metrics"] = _sglang_metrics(pm)
    else:
        # /metrics が無い(SGLang の --enable-metrics 未導入時・llama.cpp 等) →
        # SGLang の /get_load で実行中・待機リクエスト数を取得。これもない場合は None
        _reset_token_state()
        active["metrics"] = _sglang_get_load(base)

    # 直近ログ
    logs = _run(["docker", "logs", "--tail", "30", info["container"]], timeout=10)
    active["logs"] = logs.splitlines()[-30:]

    out["active"] = active
    return out


# ---------------------------------------------------------------- バックグラウンドジョブ

def _start_job(kind: str, profile: str, cmd: list[str]) -> dict:
    """バックグラウンドジョブを開始する(既存ジョブがあれば拒否)。"""
    global _job
    if _job is not None and _job["proc"].poll() is None:
        raise RuntimeError("もう1つの処理(切替/再作成)が実行中です")

    log_file = f"/tmp/dgxutil_{kind}_{int(time.time())}.log"
    lf = open(log_file, "w")
    proc = subprocess.Popen(
        cmd,
        stdout=lf,
        stderr=subprocess.STDOUT,
        stdin=subprocess.DEVNULL,
        cwd=str(COMPOSE_DIR),
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
    """モデル切替をバックグラウンド実行する。"""
    profiles = _load_profiles()
    if not profiles:
        raise ValueError(f"{COMPOSE_FILE} からプロファイル付きサービスを読み込めません")
    if profile not in profiles:
        known = ", ".join(sorted(profiles))
        raise ValueError(f"不明なプロファイル: {profile} (既知: {known})")
    return _start_job("switch", profile, ["bash", str(SWITCH_SCRIPT), profile])


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
