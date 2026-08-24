"""DGXSparkUtil モニタリング API (FastAPI)。

- GET  /api/metrics       ホストメトリクス JSON
- GET  /api/vllm/status   コンテナ状態/健全性/モデル名/metrics/ログ
- GET  /api/vllm/params   現在のパラメータ値
- POST /api/vllm/switch   モデル切替 (バックグラウンド)
- POST /api/vllm/params   パラメータ編集 + コンテナ再作成 (バックグラウンド)
- GET  /api/vllm/job      切替/再作成ジョブの進捗
- GET  /                  front/ の静的ファイル
"""

from __future__ import annotations

from pathlib import Path

from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

import metrics as host_metrics
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


class ParamsBody(BaseModel):
    profile: str
    updates: dict[str, str]


@app.get("/api/metrics")
def api_metrics() -> dict:
    return host_metrics.collect()


@app.get("/api/vllm/status")
def api_vllm_status() -> dict:
    return vllm.get_status()


@app.get("/api/vllm/params")
def api_vllm_params(profile: str) -> dict:
    try:
        return vllm.get_params(profile)
    except ValueError as e:
        raise HTTPException(400, str(e))


@app.post("/api/vllm/switch")
def api_vllm_switch(body: SwitchBody) -> dict:
    try:
        return vllm.switch_model(body.profile)
    except (ValueError, RuntimeError) as e:
        raise HTTPException(409, str(e))


@app.post("/api/vllm/params")
def api_vllm_edit_params(body: ParamsBody) -> dict:
    try:
        return vllm.edit_params(body.profile, body.updates)
    except (ValueError, RuntimeError) as e:
        raise HTTPException(409, str(e))


@app.get("/api/vllm/job")
def api_vllm_job() -> dict:
    return vllm.job_status()


# front/ の静的ファイル (index.html / chart.umd.js 等)
if FRONT_DIR.is_dir():
    app.mount(
        "/static",
        StaticFiles(directory=FRONT_DIR),
        name="static",
    )

    @app.get("/")
    def index() -> FileResponse:
        return FileResponse(FRONT_DIR / "index.html")
