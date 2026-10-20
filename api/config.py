"""プラットフォーム検出 / 環境設定の集約。

DGX Spark (GB10 / NVIDIA) と WhitebearATOM2 (R9700 / AMD) の両環境で
同じコードが動くように、ハード・パス依存の値をここに集める。

検出順:
1. 環境変数 DGXUTIL_PLATFORM ("dgx-spark" | "atom2" | "wsl") による明示上書き
2. WSL2 (/proc/version に microsoft が含まれる) なら wsl
3. nvidia-smi が PATH にあれば dgx-spark
4. amdgpu sysfs (/sys/class/drm/card*/device/gpu_busy_percent) があれば atom2
5. どれも無ければ dgx-spark 扱い(nvidia 経路が失敗しても他メトリクスは動く)

dgx-spark / atom2 は従来どおり (2 の WSL 判定は非 WSL では常に偽)。
パスは追加で環境変数 DGXUTIL_COMPOSE_DIR / DGXUTIL_RAG_DIR による上書きが可能。

モデル管理のバックエンドは backend キーで分岐する:
- "compose"  : docker-compose.yml のプロファイル + switch_models.sh (dgx-spark / atom2)
- "modelctl" : WSL の汎用ランチャー model-ctl.sh を source して切替 (docker 前提なし)
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


def _is_wsl() -> bool:
    """WSL2 実行中か (/proc/version に microsoft)。atom1 / atom2 は物理マシンなので常に偽。"""
    try:
        v = Path("/proc/version").read_text().lower()
    except OSError:
        return False
    return "microsoft" in v or "wsl" in v


def _detect_platform() -> str:
    forced = os.environ.get("DGXUTIL_PLATFORM", "").strip().lower()
    if forced in ("dgx-spark", "atom2", "wsl"):
        return forced
    if _is_wsl():
        return "wsl"
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
    # --- WSL2 (Whitebear2026 / RTX 5090) -------------------------------------
    # 追加専用。dgx-spark / atom2 の値・キーは変更していない (未設定キーは
    # _cfg() の既定値に落ちるため、既存 2 環境の解決値は従来と同一)。
    # backend=modelctl: docker-compose を使わず、汎用ランチャー model-ctl.sh
    # (14 モードを VRAM 排他で切替) を source して切替・状態取得を行う。
    "wsl": {
        "label": "Whitebear2026-WSL",
        # compose 系 API との互換のためキーは持たせる (実体は存在せず、modelctl 経路では参照しない)
        "compose_dir": "/home/cliclie/llm/compose",
        "rag_dir": "/home/cliclie/RAG/compose",
        "gpu_backend": "nvidia",
        # RAM 82GB + VRAM 32GB の分離型 → atom2 と同じ「RAM」「VRAM」2 ゲージを再利用
        "memory_mode": "discrete",
        "net_max_mbps": 1000,
        "exclusive_llm_rag": False,
        "backend": "modelctl",
        "model_ctl": "/home/cliclie/scripts/model-ctl.sh",
        "rag_enabled": False,     # WSL 側に RAG compose 環境なし → セクション非表示
        "params_editable": False,  # 設定形式が複数 (yml / json / .env) → 表示のみ
        # WSL の nvidia-smi は温度上限を公開しない (T.Limit Specification = N/A/0)。
        # RTX 5090 の GPU サーマルスレッドホルド 95°C をゲージ上限の既定として使う。
        # (dgx-spark / atom2 ではこのキー未設定 → 従来どおりフロント側既定値 100/110)
        "gpu_temp_crit_c": 95.0,
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

# モデル管理バックエンド: "compose" (dgx-spark / atom2 の既定・従来動作)
#                      | "modelctl" (WSL: model-ctl.sh を source して切替)
BACKEND: str = str(_cfg("backend", "compose")).strip().lower()
# modelctl バックエンドが source する汎用ランチャーのパス
# (環境変数 DGXUTIL_MODEL_CTL で上書き可。未設定なら空 Path)
MODEL_CTL: Path = Path(
    os.environ.get("DGXUTIL_MODEL_CTL", "").strip() or _cfg("model_ctl", "")
).expanduser()
RAG_ENABLED: bool = bool(_cfg("rag_enabled", True))
PARAMS_EDITABLE: bool = bool(_cfg("params_editable", True))
# GPU 温度ゲージの上限。プラットフォーム既定を持たない環境 (dgx-spark / atom2) は
# None のまま = 従来どおり metrics が実測上限 (amdgpu sysfs) を、無ければフロントの
# 既定値 (unified 100 / discrete 110) を使う。
GPU_TEMP_CRIT_C: float | None = _cfg("gpu_temp_crit_c", None)

# GPU サーマルガード (thermal-guard.service / root 特権はこの unit だけ) が書く
# 状態ファイル。API は読むだけで、クロック (GB10) / 電力上限 (R9700) を書く権限は持たない。
THERMAL_STATE_FILE: Path = Path(
    os.environ.get("DGXUTIL_THERMAL_STATE", "/run/thermal/state")
)


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
        # 追加キー (既存 2 環境では backend=compose / rag_enabled=true /
        # params_editable=true = 従来どおりの表示・操作)
        "backend": BACKEND,
        "rag_enabled": RAG_ENABLED,
        "params_editable": PARAMS_EDITABLE,
    }


# ---------------------------------------------------------------- Magnitude (magn)
# Magnitude はホストインストールの deb (headless serve)。compose プロファイルではないが
# docker-compose.yml にプレースホルダ service を置いて profiles 対応表に出す。
# 稼働判定は serve の API 応答 (/inference/v1/models + API key) + magnitude CLI の
# Runtime 列 (モデルが VRAM にロード済み = Ready)。/inference/v1/models は
# インストール済みモデルを常時列挙するだけでロード状態を反映しない (OS 起動直後も応答する)。
# /metrics は存在せず、/health は serve のライフサイクル状態のみを返す。

def _read_compose_env() -> dict:
    """compose_dir の .env を読む (MAGN_* 設定)。"""
    env: dict[str, str] = {}
    p = COMPOSE_DIR / ".env"
    if p.is_file():
        for ln in p.read_text().splitlines():
            ln = ln.strip()
            if ln and not ln.startswith("#") and "=" in ln:
                k, v = ln.split("=", 1)
                env[k.strip()] = v.strip()
    return env

MAGN_ENV = _read_compose_env()
MAGN_PORT = int(MAGN_ENV.get("MAGN_PORT", "10100"))
MAGN_BASE_PATH = MAGN_ENV.get("MAGN_BASE_PATH", "inference/v1")
MAGN_API_KEY = MAGN_ENV.get("MAGN_API_KEY", "")
MAGN_MODEL_ID = MAGN_ENV.get("MAGN_MODEL_ID", "qwen3.8-27b:gguf:q6")
MAGN_SYSTEMD_UNIT = MAGN_ENV.get("MAGN_SYSTEMD_UNIT", "magn-headless.service")
# Magnitude は /metrics が無いが、リクエスト統計 (~/.magnitude/serving-usage.sqlite の usage 表)
# に input/cached/output/generation_ms/first_token_ms を保持する。KVCache・E2E・TTFT・スループットをここから算出する。
MAGN_USAGE_DB = Path(
    MAGN_ENV.get("MAGN_USAGE_DB", str(Path.home() / ".magnitude" / "serving-usage.sqlite"))
).expanduser()
