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

import json
import os
import shutil
import socket
import time
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
        "model_ctl": "/home/cliclie/llm/script/model-ctl.sh",
        "rag_enabled": False,     # WSL 側に RAG compose 環境なし → セクション非表示
        "params_editable": False,  # 設定形式が複数 (yml / json / .env) → 表示のみ
        # WSL の nvidia-smi は温度上限を公開しない (T.Limit Specification = N/A/0)。
        # RTX 5090 の GPU サーマルスレッドホルド 95°C をゲージ上限の既定として使う。
        # (dgx-spark / atom2 ではこのキー未設定 → 従来どおりフロント側既定値 100/110)
        "gpu_temp_crit_c": 95.0,
        # WSL は /sys/class/thermal が空で CPU 温度が取れないため、常駐させた
        # LibreHardwareMonitor (Windows ホスト側) の JSON から取る。
        # ネットワークモードが mirrored の WSL2 では Windows ホストのポートが
        # 127.0.0.1 で共有されるため 127.0.0.1 を既定とする
        # (LAN 上の別名 whitebear2026 は WSL 内 /etc/hosts が WSL 自身 127.0.1.1 に解決
        #  して届かない)。環境変数 DGXUTIL_LHM_URL で上書き可、空にすれば無効化。
        # (dgx-spark / atom2 ではこのキー未設定 → 空 = LHM 経路は一切使わない)
        "lhm_url": "http://127.0.0.1:8085/data.json",
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

# LibreHardwareMonitor (Windows ホスト) の data.json URL。WSL 以外の環境では空文字 =
# CPU 温度の LHM 経路は無効 (metrics._read_cpu_temp_c は従来どおり sysfs のみを見る)。
# 環境変数 DGXUTIL_LHM_URL で上書き (未設定ならプラットフォーム既定値)。
LHM_URL: str = (
    os.environ.get("DGXUTIL_LHM_URL", "").strip() or str(_cfg("lhm_url", ""))
).strip()

# GPU サーマルガード (thermal-guard.service / root 特権はこの unit だけ) が書く
# 状態ファイル。API は読むだけで、クロック (GB10) / 電力上限 (R9700) を書く権限は持たない。
THERMAL_STATE_FILE: Path = Path(
    os.environ.get("DGXUTIL_THERMAL_STATE", "/run/thermal/state")
)

# ---------------------------------------------------------------- 最終稼働プロファイル
# ヘッダのモデル選択プルダウンが「モデルを選択」(プレースホルダ) になるのを避けるため、
# 最後に稼働していた LLM プロファイル / RAG ("__rag__") を永続化する。
# vllm.get_status() / rag.get_status() が稼働中に書き、どちらも停止中 (例: 再起動直後) は
# フロントが /api/vllm/status の last_active をフォールバック選択に使う。
# API は cliclie 権限で動くため home 配下 (git 管理外) に置く。
LAST_ACTIVE_FILE: Path = Path(
    os.environ.get(
        "DGXUTIL_LAST_ACTIVE", str(Path.home() / ".dgxsparkutil" / "last_active.json")
    )
)


_last_active_saved: str | None = None


def save_last_active(profile: str) -> None:
    """最終稼働プロファイルを書き込む (失敗は無視: 表示フォールバック専用)。
    値が変わったときだけ書き、毎ポーリングの繰り返し書き込みを避ける。"""
    global _last_active_saved
    if _last_active_saved == profile:
        return
    try:
        LAST_ACTIVE_FILE.parent.mkdir(parents=True, exist_ok=True)
        LAST_ACTIVE_FILE.write_text(json.dumps({"profile": profile, "ts": time.time()}))
        _last_active_saved = profile
    except OSError:
        pass


def load_last_active() -> str | None:
    try:
        return json.loads(LAST_ACTIVE_FILE.read_text()).get("profile")
    except (OSError, ValueError):
        return None


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


# ---------------------------------------------------------------- 削除したライン (2026-11-27)
# atom2 の Magnitude (magn) / Strata (strata) ラインは 2026-11-27 のモデル整理で削除した。
# 稼働ラインは hipfire / qwen3vl / clef の 3 つ (+ RAG embedding) で、いずれも API key を
# 使わないため compose/.env を読む機構 (旧 _read_compose_env / MAGN_* / STRATA_API_KEY) は
# 不要になった。再導入する場合は git タグ pre-model-cleanup-20261127 を参照すること。
