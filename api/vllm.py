"""vLLM 状態モニタリング / モデル切替 / パラメータ編集。

- 状態: docker ps/inspect + vLLM /health, /v1/models, /metrics + docker logs
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

# switch_models.sh と同じ対応表 (profile -> service/container/port)
PROFILES: dict[str, dict] = {
    "nemotron": {"service": "nemotron120b", "container": "vllm-nemotron120b", "port": 8000},
    "qwen": {"service": "qwen72b", "container": "vllm-qwen72b", "port": 8001},
    "laguna": {"service": "laguna72b", "container": "vllm-laguna72b", "port": 8002},
    "qwen36": {"service": "qwen36_35b", "container": "vllm-qwen36-35b", "port": 8003},
    "muse": {"service": "muse_glimmer", "container": "llama-muse-glimmer", "port": 8004},
    "qwen38": {"service": "qwen38_27b", "container": "vllm-qwen38-27b", "port": 8005},
    "qwen38bf16": {"service": "qwen38_27b_bf16", "container": "vllm-qwen38-27b-bf16", "port": 8006},
    "qwen38nvfp4": {"service": "qwen38_27b_nvfp4", "container": "vllm-qwen38-27b-nvfp4", "port": 8007},
}

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
    ラベルを除去して系列名のみで集約する(同一系列の複数ラベルは加算しない、
    単一エンジン構成のため初出を採用)。
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
        if name not in m:
            m[name] = v
    return m


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


def get_status() -> dict:
    """稼働中モデルの状態 + vLLM メトリクスを返す。"""
    out: dict = {"containers": [], "active": None}

    # 各コンテナの稼働状態
    running_profile = None
    for profile, info in PROFILES.items():
        st = _container_state(info["container"])
        if st.get("running"):
            running_profile = profile
        out["containers"].append({"profile": profile, "container": info["container"], **st})

    if running_profile is None:
        return out

    info = PROFILES[running_profile]
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

    # vLLM 組み込みメトリクス (llama.cpp は無い)
    # vLLM 26.x の系列名: e2e 相当は request_inference_time_seconds、
    # KV キャッシュは kv_cache_usage_perc
    mt = _http_get(f"{base}/metrics", timeout=5)
    if mt:
        pm = _parse_vllm_metrics(mt)

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

        # トークンスループット(カウンター差分)
        tokens = (
            pm.get("vllm:prompt_tokens_total", 0.0)
            + pm.get("vllm:generation_tokens_total", 0.0)
        )
        now = time.time()
        if _prev_metrics["tokens"] is not None:
            dt = max(now - _prev_metrics["ts"], 1e-9)
            active["metrics"]["tokens_per_s"] = max(0.0, (tokens - _prev_metrics["tokens"]) / dt)
        _prev_metrics["ts"], _prev_metrics["tokens"] = now, tokens
    else:
        active["metrics"] = None

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
            tail = [
                re.sub(r"\x1b\[[0-9;?]*[A-Za-z]|\r", "", ln).rstrip()
                for ln in f.readlines()[-12:]
            ]
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
    if profile not in PROFILES:
        raise ValueError(f"不明なプロファイル: {profile}")
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
    if profile not in PROFILES:
        raise ValueError(f"不明なプロファイル: {profile}")
    service = PROFILES[profile]["service"]
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
    if profile not in PROFILES:
        raise ValueError(f"不明なプロファイル: {profile}")
    service = PROFILES[profile]["service"]

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
