"""RAG環境 (sociax-rag) の状態モニタリング / 起動 / 停止。

本機の RAG(embedding vLLM + Qdrant)を Web UI から起動/停止できるようにする。

- 状態: docker compose ps / inspect + embedding /v1/models, qdrant REST の健全性
- 起動: docker compose up -d (バックグラウンドジョブ)
  ※ 排他環境 (atom2 / VRAM 32GB) では switch_models.sh rag を経由し、
    稼働中 LLM を停止してから embedding を起動する(Qdrant は up -d で補完)
- 停止: docker compose stop (バックグラウンドジョブ。Qdrant は volume に永続化済み)
- ジョブ状態は vllm.py の _job と独立(モデル切替と RAG 操作の並行実行を許可)
"""

from __future__ import annotations

import json
import re
import subprocess
import time
from datetime import datetime
from pathlib import Path

import config

# パスは config (プラットフォーム検出) に集約
RAG_DIR = config.RAG_DIR
COMPOSE_FILE = RAG_DIR / "compose.yaml"
ENV_FILE = RAG_DIR / ".env"

# サービスと健全性チェック用 URL (ポートは .env から取得、既定値は compose.yaml と一致)
SERVICES = {
    "embedding": {"port_key": "EMBEDDING_PORT", "port_default": 8010, "health_path": "/v1/models"},
    "qdrant": {"port_key": "QDRANT_REST_PORT", "port_default": 6333, "health_path": "/"},
}


def _env_value(key: str, default: str) -> str:
    """.env から key の値を返す(無ければ default)。"""
    try:
        for ln in ENV_FILE.read_text().splitlines():
            if ln.startswith(key + "="):
                return ln.split("=", 1)[1].strip()
    except OSError:
        pass
    return default


def _health_url(service: str) -> str:
    cfg = SERVICES[service]
    port = _env_value(cfg["port_key"], str(cfg["port_default"]))
    return f"http://localhost:{port}{cfg['health_path']}"


def _port_of(service: str) -> int:
    cfg = SERVICES[service]
    try:
        return int(_env_value(cfg["port_key"], str(cfg["port_default"])))
    except ValueError:
        return cfg["port_default"]


def _compose_cmd(*args: str) -> list[str]:
    return ["docker", "compose", "--env-file", str(ENV_FILE), "-f", str(COMPOSE_FILE), *args]


def _run(cmd: list[str], timeout: float = 10) -> str:
    """コマンドを実行して stdout を返す(失敗時は空文字列)。"""
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
        return r.stdout
    except (subprocess.SubprocessError, OSError):
        return ""


def _http_ok(url: str, timeout: float = 3) -> bool:
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


def _container_names() -> dict[str, str]:
    """docker compose ps から {service: container_name} を返す。"""
    out: dict[str, str] = {}
    txt = _run(_compose_cmd("ps", "--format", "{{.Service}}\t{{.Name}}"))
    for ln in txt.splitlines():
        parts = ln.split("\t")
        if len(parts) == 2:
            out[parts[0]] = parts[1]
    return out


def _container_state(name: str) -> dict:
    """コンテナの稼働状態 / 稼働時間 / 再起動回数を返す。"""
    try:
        r = subprocess.run(
            ["docker", "inspect", name],
            capture_output=True,
            text=True,
            timeout=10,
        )
        c = json.loads(r.stdout)[0]
        # 稼働時間の起点は State.StartedAt(最後に起動した日時)。
        # Created はコンテナ作成時のみで、再起動しても更新されないため使わない。
        started = c.get("State", {}).get("StartedAt", "")
        now = time.time()
        uptime = None
        if c.get("State", {}).get("Running") and started:
            try:
                t = datetime.fromisoformat(started.replace("Z", "+00:00"))
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
    except (subprocess.SubprocessError, OSError, ValueError, IndexError, KeyError):
        return {}


def embedding_running() -> bool:
    """embedding コンテナが稼働中か (vllm.py の排他警告用)。"""
    name = _container_names().get("embedding")
    return bool(name and _container_state(name).get("running"))


def _llm_running() -> bool:
    """LLM コンテナのいずれかが稼働中か (排他環境の状態表示用)。

    遅延 import: vllm.py は get_status で rag を参照する循環構成。
    """
    try:
        import vllm

        profiles = vllm._load_profiles()
        return any(
            _container_state(p["container"]).get("running") for p in profiles.values()
        )
    except Exception:
        return False


# ---------------------------------------------------------------- 状態

def _switching_info() -> dict | None:
    """実行中ジョブがある場合、kind と起動準備完了フラグを返す。

    フロントの状態表示で「起動中…/停止中…」を区別する。
    ready は起動ジョブのみ両サービスの API が応答するかで判定する。
    """
    if _job is None or _job["proc"].poll() is not None:
        return None
    kind = _job["kind"]
    ready = False
    if kind == "start":
        ready = all(_http_ok(_health_url(svc)) for svc in SERVICES)
    return {"kind": kind, "ready": ready}


def get_status() -> dict:
    """RAG 各サービスの状態 + 全体状態を返す。"""
    names = _container_names()
    out: dict = {"services": {}, "running": False, "healthy": False, "switching": None}
    for svc in SERVICES:
        name = names.get(svc)
        st = _container_state(name) if name else {}
        running = bool(st.get("running"))
        out["services"][svc] = {
            "container": name,
            "port": _port_of(svc),
            "running": running,
            "health": _http_ok(_health_url(svc)) if running else False,
            "uptime_str": st.get("uptime_str"),
            "restart_count": st.get("restart_count"),
        }
        out["running"] = out["running"] or running
    out["healthy"] = out["running"] and all(
        s["running"] and s["health"] for s in out["services"].values()
    )
    out["switching"] = _switching_info()
    # 排他環境 (atom2): LLM 稼働中かつ embedding 停止中なら「LLM 排他で停止」状態
    if config.EXCLUSIVE_LLM_RAG:
        llm = _llm_running()
        out["llm_running"] = llm
        out["blocked_by_llm"] = llm and not out["services"]["embedding"]["running"]
    return out


# ---------------------------------------------------------------- バックグラウンドジョブ

_job: dict | None = None


def _start_job(kind: str, cmd: list[str]) -> dict:
    """バックグラウンドジョブを開始する(既存ジョブがあれば拒否)。"""
    global _job
    if _job is not None and _job["proc"].poll() is None:
        raise RuntimeError("もう1つの処理(起動/停止)が実行中です")

    log_file = f"/tmp/dgxutil_rag_{kind}_{int(time.time())}.log"
    lf = open(log_file, "w")
    proc = subprocess.Popen(
        cmd,
        stdout=lf,
        stderr=subprocess.STDOUT,
        stdin=subprocess.DEVNULL,
        cwd=str(RAG_DIR),
    )
    _job = {
        "kind": kind,
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
        tail = [ln for ln in raw if ln.strip()][-12:]
        # switch_models.sh rag の上書き表示フレーム(ヘッダー+ログ4行)はファイル上では
        # 毎秒追記されるため、末尾に複数コピーが残る。最後の === ヘッダー行以降の
        # 1フレーム分のみを保持して、ヘッダーが1行だけになるようにする(vllm.py と同一)。
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
            "elapsed_s": round(time.time() - _job["started_at"]),
            "success": None,
            "log_tail": tail,
        }
    return {
        "running": False,
        "kind": _job["kind"],
        "elapsed_s": round(time.time() - _job["started_at"]),
        "success": code == 0,
        "exit_code": code,
        "log_tail": tail,
    }


def start_rag() -> dict:
    """RAG環境を起動する(バックグラウンド)。既に稼働中の場合は compose が何もしない。

    排他環境 (atom2) では switch_models.sh rag を実行し、稼働中 LLM を停止してから
    embedding を起動する(スクリプトが API 応答まで待機)。完了後に qdrant も補完起動。
    """
    if not COMPOSE_FILE.is_file():
        raise ValueError(f"{COMPOSE_FILE} が見つかりません")
    switch = config.COMPOSE_DIR / "switch_models.sh"
    if config.EXCLUSIVE_LLM_RAG and switch.is_file():
        cmd = [
            "bash", "-c",
            "bash " + str(switch) + " rag && "
            + " ".join(_compose_cmd("up", "-d")),
        ]
        return _start_job("start", cmd)
    return _start_job("start", _compose_cmd("up", "-d"))


def stop_rag() -> dict:
    """RAG環境を停止する(バックグラウンド)。Qdrant データは volume に永続化済み。"""
    if not COMPOSE_FILE.is_file():
        raise ValueError(f"{COMPOSE_FILE} が見つかりません")
    names = _container_names()
    running = [
        svc
        for svc in SERVICES
        if names.get(svc) and _container_state(names[svc]).get("running")
    ]
    if not running:
        raise ValueError("RAG環境は稼働していません")
    return _start_job("stop", _compose_cmd("stop"))