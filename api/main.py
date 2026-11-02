"""DGXSparkUtil モニタリング API (FastAPI)。

- GET  /api/metrics       ホストメトリクス JSON
- GET  /api/vllm/status   コンテナ状態/健全性/モデル名/metrics/ログ
- GET  /api/vllm/params   現在のパラメータ値
- GET  /api/vllm/cline    Cline 設定値 (稼働中モデル)
- GET  /api/vllm/log      モデルコンテナの docker logs (実行ログダイアログ用)
- POST /api/vllm/switch   モデル切替 (バックグラウンド)
- POST /api/vllm/stop     モデル停止 (バックグラウンド)
- POST /api/vllm/params   パラメータ編集 + コンテナ再作成 (バックグラウンド)
- GET  /api/vllm/job      切替/再作成/停止ジョブの進捗
- GET  /api/rag/status    RAG環境 (sociax-rag) の状態
- POST /api/rag/start     RAG環境起動 (バックグラウンド)
- POST /api/rag/stop      RAG環境停止 (バックグラウンド)
- GET  /api/rag/job       RAG 起動/停止ジョブの進捗
- GET  /api/thermal       GPU サーマルガード (root unit) の状態
- GET  /api/page-version  配信中 index.html の ETag (フロントの自動リロード判定用)
- GET  /                  front/ の静的ファイル (Cache-Control: no-cache)
"""

from __future__ import annotations

import hashlib
import json
import threading
import time
from collections import deque
from pathlib import Path

from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import Response
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

import config
import metrics as host_metrics
import rag
import vllm

BASE_DIR = Path(__file__).resolve().parent
FRONT_DIR = BASE_DIR.parent / "front"

app = FastAPI(title="DGXSparkUtil Monitor API")

# 同一 LAN 内の別ホスト(開発用)からの直接アクセスを許可
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)


class SwitchBody(BaseModel):
    profile: str


# ---------------------------------------------------------------- 時系列履歴
# フロントのページリロード後に線グラフを最大30分だけ遡って表示するためのバックフィル用。
# サーバー側で5秒間隔・30分(360点)だけ保持する(ブラウザが開いていなくても蓄積される)。
HISTORY_INTERVAL_S = 5
_history: deque[dict] = deque(maxlen=1800 // HISTORY_INTERVAL_S)


def _history_sampler() -> None:
    while True:
        try:
            _history.append(host_metrics.collect())
        except Exception:
            pass
        time.sleep(HISTORY_INTERVAL_S)


threading.Thread(target=_history_sampler, daemon=True).start()


@app.get("/api/history")
def api_history() -> list[dict]:
    return list(_history)


class ParamsBody(BaseModel):
    profile: str
    updates: dict[str, str]


@app.get("/api/metrics")
def api_metrics() -> dict:
    return host_metrics.collect()


@app.get("/api/platform")
def api_platform() -> dict:
    """プラットフォーム情報 (フロントの表示切替用)。

    net_max_mbps は既定値 (config.PLATFORMS) ではなく実リンク速度を優先する。
    """
    info = config.info()
    info["net_max_mbps"] = host_metrics.net_gauge_max_mbps()
    return info


@app.get("/api/vllm/status")
def api_vllm_status() -> dict:
    return vllm.get_status()


@app.get("/api/vllm/params")
def api_vllm_params(profile: str) -> dict:
    try:
        return vllm.get_params(profile)
    except ValueError as e:
        raise HTTPException(400, str(e))


@app.get("/api/vllm/cline")
def api_vllm_cline() -> dict:
    try:
        return vllm.get_cline_config()
    except ValueError as e:
        raise HTTPException(409, str(e))


@app.post("/api/vllm/switch")
def api_vllm_switch(body: SwitchBody) -> dict:
    try:
        return vllm.switch_model(body.profile)
    except (ValueError, RuntimeError) as e:
        raise HTTPException(409, str(e))


@app.post("/api/vllm/stop")
def api_vllm_stop(body: SwitchBody) -> dict:
    try:
        return vllm.stop_model(body.profile)
    except (ValueError, RuntimeError) as e:
        raise HTTPException(409, str(e))


@app.post("/api/vllm/params")
def api_vllm_edit_params(body: ParamsBody) -> dict:
    try:
        return vllm.edit_params(body.profile, body.updates)
    except (ValueError, RuntimeError) as e:
        raise HTTPException(409, str(e))


@app.get("/api/vllm/log")
def api_vllm_log(profile: str, tail: int = 500) -> dict:
    try:
        return vllm.get_log(profile, tail)
    except ValueError as e:
        raise HTTPException(400, str(e))


@app.get("/api/vllm/job")
def api_vllm_job() -> dict:
    return vllm.job_status()


@app.get("/api/rag/status")
def api_rag_status() -> dict:
    return rag.get_status()


@app.post("/api/rag/start")
def api_rag_start() -> dict:
    try:
        return rag.start_rag()
    except (ValueError, RuntimeError) as e:
        raise HTTPException(409, str(e))


@app.post("/api/rag/stop")
def api_rag_stop() -> dict:
    try:
        return rag.stop_rag()
    except (ValueError, RuntimeError) as e:
        raise HTTPException(409, str(e))


@app.get("/api/rag/job")
def api_rag_job() -> dict:
    return rag.job_status()


@app.get("/api/thermal")
def api_thermal() -> dict:
    """GPU サーマルガード (thermal-guard.service) の状態を返す。

    状態ファイルは root 特権の unit が書く。API は読むだけで、nvidia-smi -lgc
    (GB10 / clock モード) も power1_cap (R9700 / power モード) も叩かない。
    unit 停止時は enabled=false。mode キーでどちらかを判別できる。
    """
    try:
        return json.loads(config.THERMAL_STATE_FILE.read_text())
    except (OSError, ValueError):
        return {"enabled": False}


# front/ の静的ファイル (index.html / chart.umd.js 等)
def _page_etag(p: Path) -> str:
    """index.html の mtime+size から ETag を算出 (starlette の FileResponse と同一方式)。"""
    st = p.stat()
    return hashlib.sha1(f"{st.st_mtime_ns}-{st.st_size}".encode()).hexdigest()


@app.get("/api/page-version")
def api_page_version() -> dict:
    """配信中 index.html の ETag。フロントはページ自身に注入された ETag と比較し、
    違えば location.reload() で最新ページへ自動更新する (キャッシュの古いタブ対策)。"""
    return {"etag": _page_etag(FRONT_DIR / "index.html")}


if FRONT_DIR.is_dir():
    app.mount(
        "/static",
        StaticFiles(directory=FRONT_DIR),
        name="static",
    )

    @app.get("/")
    def index() -> Response:
        # Cache-Control: no-cache でブラウザは毎回サーバーに再検証 (変更なければ 304)。
        # ページを開くたびに最新 index.html を取得し、手動リロードを不要にする。
        # __PAGE_ETAG__ には配信中ページの ETag を注入し、自動リロード判定の基準にする。
        p = FRONT_DIR / "index.html"
        etag = _page_etag(p)
        # 先頭 1 箇所 (const PAGE_ETAG = "__PAGE_ETAG__") のみ置換。
        # 防御チェックの PAGE_ETAG === "__PAGE_ETAG__" リテラルはそのまま残す
        # (全置換するとチェックが実 ETag 比較になり常に早期 return する)。
        html = p.read_text().replace("__PAGE_ETAG__", etag, 1)
        return Response(
            html,
            media_type="text/html; charset=utf-8",
            headers={"Cache-Control": "no-cache", "ETag": f'"{etag}"'},
        )
