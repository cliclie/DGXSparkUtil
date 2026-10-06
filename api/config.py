"""プラットフォーム検出 / 環境設定の集約。

DGX Spark (GB10 / NVIDIA) と WhitebearATOM2 (R9700 / AMD) の両環境で
同じコードが動くように、ハード・パス依存の値をここに集める。

検出順:
1. 環境変数 DGXUTIL_PLATFORM ("dgx-spark" | "atom2") による明示上書き
2. nvidia-smi が PATH にあれば dgx-spark
3. amdgpu sysfs (/sys/class/drm/card*/device/gpu_busy_percent) があれば atom2
4. どちらも無ければ dgx-spark 扱い(nvidia 経路が失敗しても他メトリクスは動く)

パスは追加で環境変数 DGXUTIL_COMPOSE_DIR / DGXUTIL_RAG_DIR による上書きが可能。
"""

from __future__ import annotations

import os
import shutil
import socket
from pathlib import Path

# ---------------------------------------------------------------- 検出

def _has_nvidia() -> bool:
    return shutil.which("nvidia-smi") is not None


def _has_amdgpu() -> bool:
    return bool(list(Path("/sys/class/drm").glob("card[0-9]*/device/gpu_busy_percent")))


def _detect_platform() -> str:
    forced = os.environ.get("DGXUTIL_PLATFORM", "").strip().lower()
    if forced in ("dgx-spark", "atom2"):
        return forced
    if _has_nvidia():
        return "dgx-spark"
    if _has_amdgpu():
        return "atom2"
    return "dgx-spark"


PLATFORM = _detect_platform()

# ---------------------------------------------------------------- プラットフォーム別設定

PLATFORMS: dict[str, dict] = {
    "dgx-spark": {
        "label": "DGX Spark",
        "compose_dir": "/home/cliclie/llm/compose",
        "rag_dir": "/home/cliclie/llm/compose/sociax-rag",
        "gpu_backend": "nvidia",
        # unified: CPU/GPU 共有メモリ(GB10 128GB) → 「統合メモリ」ゲージ 1 枚
        # discrete: RAM と VRAM 別 → 「RAM」「VRAM」ゲージ 2 枚
        "memory_mode": "unified",
        "net_max_mbps": 10000,   # ゲージ上限の既定値 (実リンク速度が取れない時のフォールバック)
        # LLM と RAG embedding の排他有無 (atom2 は VRAM 32GB のため排他)
        "exclusive_llm_rag": False,
    },
    "atom2": {
        "label": "WhitebearATOM2",
        "compose_dir": "/home/cliclie/LLM/compose",
        "rag_dir": "/home/cliclie/RAG/compose",
        "gpu_backend": "amdgpu",
        "memory_mode": "discrete",
        "net_max_mbps": 1000,    # ゲージ上限の既定値 (実リンク速度が取れない時のフォールバック)
        "exclusive_llm_rag": True,
    },
}


def _cfg(key: str, default):
    return PLATFORMS.get(PLATFORM, PLATFORMS["dgx-spark"]).get(key, default)


def _path_from_env(env_key: str, cfg_key: str) -> Path:
    override = os.environ.get(env_key, "").strip()
    if override:
        return Path(override).expanduser().resolve()
    return Path(_cfg(cfg_key, "")).resolve()


# ---------------------------------------------------------------- 公開値

COMPOSE_DIR = _path_from_env("DGXUTIL_COMPOSE_DIR", "compose_dir")
RAG_DIR = _path_from_env("DGXUTIL_RAG_DIR", "rag_dir")

GPU_BACKEND: str = _cfg("gpu_backend", "nvidia")
MEMORY_MODE: str = _cfg("memory_mode", "unified")
NET_MAX_MBPS: int = _cfg("net_max_mbps", 10000)
EXCLUSIVE_LLM_RAG: bool = bool(_cfg("exclusive_llm_rag", False))
LABEL: str = _cfg("label", "DGX Spark")


def info() -> dict:
    """フロントの表示切替用 (/api/platform)。"""
    return {
        "platform": PLATFORM,
        "label": LABEL,
        "hostname": socket.gethostname(),
        "gpu_backend": GPU_BACKEND,
        "memory_mode": MEMORY_MODE,
        "net_max_mbps": NET_MAX_MBPS,
        "exclusive_llm_rag": EXCLUSIVE_LLM_RAG,
        "compose_dir": str(COMPOSE_DIR),
        "rag_dir": str(RAG_DIR),
    }