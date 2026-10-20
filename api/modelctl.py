"""modelctl バックエンド (WSL2) — model-ctl.sh を source したブリッジの読み取り側。

atom1 (dgx-spark) / atom2 の compose バックエンド (docker-compose.yml + switch_models.sh)
と同じ形のプロファイル表を、docker を使わずに WSL の汎用ランチャーから構築する。

- ブリッジ: wsl_modelctl_bridge.sh (本ディレクトリ) が model-ctl.sh を source して
  タブ区切りで list / status / params を返す。model-ctl.sh 本体は変更しない。
- 切替・停止は vllm.py 側のバックグラウンドジョブ (_start_job) に委ね、
  ここではコマンド列の組み立てと状態読み取りのみを行う。
"""

from __future__ import annotations

import subprocess
import time
from pathlib import Path

import config

BRIDGE = Path(__file__).resolve().parent / "wsl_modelctl_bridge.sh"

# 2 秒ポーリングでも低コストに抑えるための短い TTL (list 実行は ~0.6 秒)
_TTL_S = 1.5
_cache: dict[str, dict] = {}
_cache_ts: float = 0.0
_cache_ok: bool = False


def available() -> bool:
    """ブリッジと model-ctl.sh が揃っているか。"""
    return BRIDGE.is_file() and config.MODEL_CTL.is_file()


def _run_bridge(args: list[str], timeout: float = 20.0) -> tuple[int, str, str]:
    """ブリッジを実行して (returncode, stdout, stderr) を返す。"""
    try:
        r = subprocess.run(
            ["bash", str(BRIDGE), *args],
            capture_output=True,
            text=True,
            timeout=timeout,
            stdin=subprocess.DEVNULL,
        )
        return r.returncode, r.stdout, r.stderr
    except subprocess.TimeoutExpired:
        return 124, "", f"bridge timeout ({timeout}s)"
    except OSError as e:
        return 127, "", str(e)


def _parse_row(ln: str) -> dict | None:
    """list / status の 1 行 (8 列タブ区切り) を dict にする。"""
    cols = ln.split("\t")
    if len(cols) < 8:
        return None
    mode, label, port, dir_, ready, running, engine, log = cols[:8]
    try:
        port_i = int(port)
    except ValueError:
        port_i = None
    # vllm.py の compose 版プロファイル表と同じキー (service / container / port) を保ち、
    # modelctl 固有の追加キーを並べる。container はモード名そのもの (docker ではない)。
    return {
        "service": mode,
        "container": mode,
        "port": port_i,
        "mode": mode,
        "label": label,
        "dir": dir_,
        "ready": ready == "1",
        "running": running == "1",
        "engine": engine,
        "log": log,
    }


def profiles(force: bool = False) -> dict[str, dict]:
    """{mode: {...}} を返す (TTL キャッシュ)。失敗時は空 dict。"""
    global _cache, _cache_ts, _cache_ok
    now = time.time()
    if not force and _cache_ok and now - _cache_ts < _TTL_S:
        return _cache
    code, out, _err = _run_bridge(["list"])
    result: dict[str, dict] = {}
    if code == 0:
        for ln in out.splitlines():
            row = _parse_row(ln)
            if row:
                result[row["mode"]] = row
    _cache = result
    _cache_ts = now
    _cache_ok = code == 0 and bool(result)
    return result if _cache_ok else (_cache or result)


def running_profiles() -> list[str]:
    """稼働中モードの一覧 (Strata は代表 1 件に集約済み)。"""
    return [m for m, p in profiles().items() if p["running"]]


_params_cache: dict[str, tuple[float, dict[str, str]]] = {}
_PARAMS_TTL_S = 5.0


def params(mode: str, force: bool = False) -> dict[str, str]:
    """表示用パラメータ (key -> value)。model-ctl.sh の既存アクセサが正本。

    ブリッジ起動を毎回するのは重い (API キー・LAN IP 読み込みあり) ため、
    短 TTL でキャッシュする。
    """
    now = time.time()
    hit = _params_cache.get(mode)
    if hit and not force and now - hit[0] < _PARAMS_TTL_S:
        return hit[1]
    code, out, _err = _run_bridge(["params", mode])
    result: dict[str, str] = {}
    if code == 0:
        for ln in out.splitlines():
            k, sep, v = ln.partition("\t")
            if sep:
                result[k] = v
        _params_cache[mode] = (now, result)
    elif hit:
        return hit[1]
    return result


def switch_cmd(mode: str) -> list[str]:
    return ["bash", str(BRIDGE), "switch", mode]


def stop_cmd(mode: str) -> list[str]:
    return ["bash", str(BRIDGE), "stop", mode]


def unload_cmd() -> list[str]:
    return ["bash", str(BRIDGE), "unload"]


def tail_log(path: str, tail: int) -> list[str]:
    """モデルサーバーのログファイル末尾 tail 行 (docker logs 代替)。

    改行は落として返す (compose 側の docker logs 出力 = splitlines() と同じ形)。
    行末改行を残したまま "\\n".join すると空行が混ざり、ログ解析の継続行結合で
    語間に余分な空白が入る (TabbyAPI の "· " 区切り正規表現が外れる)。
    """
    p = Path(path) if path else None
    if not p or not p.is_file():
        return []
    try:
        with p.open(errors="replace") as f:
            return [ln.rstrip("\n") for ln in f.readlines()[-tail:]]
    except OSError:
        return []


def read_log_text(path: str, tail: int = 300) -> str:
    """メトリクス解析用にログ末尾 tail 行を 1 文字列で返す。"""
    return "\n".join(tail_log(path, tail))


def uptime_str(mode: str) -> str | None:
    """起動ログファイルの mtime から稼働時間文字列を算出する (docker inspect 代替)。

    稼働中でない場合は None。model-ctl.sh は起動のたびに logs/ctl-<ts>.log を
    新規作成するため、その mtime が起動時刻になる。
    """
    p = profiles().get(mode)
    if not p or not p["running"]:
        return None
    log = p.get("log") or ""
    f = Path(log) if log else None
    if not f or not f.is_file():
        return None
    try:
        started = f.stat().st_mtime
    except OSError:
        return None
    secs = int(max(0.0, time.time() - started))
    return f"{secs // 3600:02d}:{(secs % 3600) // 60:02d}:{secs % 60:02d}"