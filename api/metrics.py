"""ホストメトリクス収集(読み取り専用)。

データ源:
- CPU負荷: /proc/stat の idle 差分
- CPU温度: /sys/class/thermal/thermal_zone* (acpitz 優先、x86_pkg_temp フォールバック、ミリ℃)
- メモリ: /proc/meminfo (DGX Spark は統合メモリ、atom2 は RAM。VRAM は GPU 側で取得)
- GPU負荷/温度/電力/クロック: config.GPU_BACKEND に応じ nvidia-smi または amdgpu sysfs
- ストレージ使用率: shutil.disk_usage
- ストレージ負荷: /proc/diskstats 差分 (ルート FS の親デバイスを自動導出)
- ネットワーク負荷: /proc/net/dev 差分 (デフォルトルートIF) / 上限=リンク速度
  (/sys/class/net/<iface>/speed、無ければ ethtool、さらに無ければ config.NET_MAX_MBPS)
"""

from __future__ import annotations

import os
import re
import shutil
import subprocess
import time
from pathlib import Path

import config

# 1 セクタ = 512 バイト
_SECTOR = 512

_DISK_MOUNT = "/"


def _root_disk_name() -> str:
    """ルートファイルシステムのブロックデバイス親名を導出する (例: /dev/nvme0n1p2 → nvme0n1)。

    nvme/mmc のパーティション番号サフィックス (pN) と SATA 系の末尾数字 (sda1) を除去する。
    tmpfs 等の場合は "nvme0n1" にフォールバック(見つからなければ diskstats 系は None になる)。
    """
    dev = ""
    try:
        with open("/proc/mounts") as f:
            for line in f:
                parts = line.split()
                if len(parts) >= 2 and parts[1] == "/":
                    dev = parts[0]
                    break
    except OSError:
        pass
    if not dev.startswith("/dev/"):
        return "nvme0n1"
    name = Path(dev).name
    if re.search(r"p\d+$", name):     # nvme0n1p2 / mmcblk0p1 → pN サフィックスのみ除去
        name = re.sub(r"p\d+$", "", name)
    else:                              # sda1 / vda2 → 末尾数字のみ除去
        name = re.sub(r"\d+$", "", name)
    return name or "nvme0n1"


_DISK_NAME = _root_disk_name()

# 前回サンプル(差分計算用)
_prev: dict = {
    "cpu_idle": None,
    "cpu_total": None,
    "disk": None,  # (ts, reads, writes, sectors_read, sectors_written, ms_read, ms_write)
    "net": None,   # (ts, rx_bytes, tx_bytes)
}


def _read_cpu_times() -> tuple[int, int]:
    """/proc/stat の CPU 集計行から (idle, total) を返す。"""
    with open("/proc/stat") as f:
        fields = f.readline().split()[1:]
    vals = [int(x) for x in fields]
    idle = vals[3] + vals[4]  # idle + iowait
    return idle, sum(vals)


def _read_disk_stats():
    """/proc/diskstats から対象デバイスの統計を返す。

    返り値: (reads, writes, sectors_read, sectors_written, ms_read, ms_write)
    """
    with open("/proc/diskstats") as f:
        for line in f:
            parts = line.split()
            if len(parts) >= 14 and parts[2] == _DISK_NAME:
                return (
                    int(parts[3]),   # reads completed
                    int(parts[7]),   # writes completed
                    int(parts[5]),   # sectors read
                    int(parts[9]),   # sectors written
                    int(parts[6]),   # time spent reading (ms)
                    int(parts[10]),  # time spent writing (ms)
                )
    return None


def _default_iface() -> str | None:
    """/proc/net/route からデフォルトルート(宛先 00000000)のインターフェース名を返す。

    docker の br-*/veth*/docker0 は内部トラフィックのため、外部通信を実際に
    担うデフォルトルートIFのみを計測対象とする。
    """
    try:
        with open("/proc/net/route") as f:
            for line in f:
                parts = line.split()
                if len(parts) >= 2 and parts[1] == "00000000":
                    return parts[0]
    except OSError:
        pass
    return None


def _read_net_stats(iface: str):
    """/proc/net/dev から指定インターフェースの (rx_bytes, tx_bytes) を返す。"""
    try:
        with open("/proc/net/dev") as f:
            for line in f:
                if ":" not in line:
                    continue
                name, data = line.split(":", 1)
                if name.strip() != iface:
                    continue
                fields = data.split()
                return int(fields[0]), int(fields[8])  # rx bytes, tx bytes
    except (OSError, ValueError, IndexError):
        pass
    return None


# リンク速度キャッシュ (リンク再ネゴシエーション追従のため TTL で再取得)
_LINK_SPEED_TTL_S = 10.0
_link_speed_cache: dict = {"iface": None, "ts": 0.0, "mbps": None}


def link_speed_mbps() -> int | None:
    """デフォルトルート IF の実リンク速度 (Mbps) — `ethtool` の Speed と同値。

    1. /sys/class/net/<iface>/speed を優先読取 (root 権限不要。link down だと 0/エラー)
    2. 取れなければ `ethtool <iface>` の "Speed: 1000Mb/s" 行をパース
    どちらも失敗時は None (呼び出し側で config.NET_MAX_MBPS の既定値にフォールバック)。
    """
    iface = _default_iface()
    if iface is None:
        return None
    now = time.monotonic()
    cache = _link_speed_cache
    if (cache["iface"] == iface and cache["mbps"] is not None
            and now - cache["ts"] < _LINK_SPEED_TTL_S):
        return cache["mbps"]  # type: ignore[return-value]

    mbps: int | None = None
    try:
        speed = int(Path(f"/sys/class/net/{iface}/speed").read_text().strip())
        if speed > 0:
            mbps = speed
    except (OSError, ValueError):
        mbps = None
    if mbps is None:
        try:
            r = subprocess.run(["ethtool", iface], capture_output=True,
                               text=True, timeout=2.0)
            m = re.search(r"Speed:\s*(\d+(?:\.\d+)?)\s*([MG])b/s", r.stdout)
            if m:
                mbps = int(float(m.group(1)) * (1000 if m.group(2) == "G" else 1))
        except (OSError, ValueError, subprocess.SubprocessError):
            mbps = None
    cache.update(iface=iface, ts=now, mbps=mbps)
    return mbps


def net_gauge_max_mbps() -> int:
    """ネットワーク負荷ゲージの上限 (100% 相当)。実測リンク速度、無ければ既定値。"""
    return link_speed_mbps() or int(config.NET_MAX_MBPS)


def _read_cpu_temp_c() -> float | None:
    """CPU温度(℃)。acpitz ゾーン全体の最高値、無ければ x86_pkg_temp にフォールバック。"""
    for want in ("acpitz", "x86_pkg_temp"):
        temps = []
        for z in sorted(Path("/sys/class/thermal").glob("thermal_zone*")):
            try:
                if (z / "type").read_text().strip() != want:
                    continue
                temps.append(int((z / "temp").read_text()) / 1000.0)
            except (OSError, ValueError):
                continue
        if temps:
            return max(temps)
    return None


def _read_gpu_nvidia() -> dict:
    """nvidia-smi で GPU メトリクスを取得する (DGX Spark / GB10)。"""
    try:
        r = subprocess.run(
            [
                "nvidia-smi",
                "--query-gpu=utilization.gpu,temperature.gpu,power.draw,clocks.sm,pstate",
                "--format=csv,noheader,nounits",
            ],
            capture_output=True,
            text=True,
            timeout=5,
        )
        if r.returncode != 0:
            return {}
        u, t, p, c, ps = [x.strip() for x in r.stdout.split(",")]
        return {
            "gpu_vendor": "nvidia",
            "gpu_load_pct": float(u),
            "gpu_temp_c": float(t),
            "gpu_power_w": float(p),
            "gpu_clock_mhz": float(c),
            "gpu_pstate": ps,
        }
    except (subprocess.SubprocessError, ValueError):
        return {}


_amdgpu_dev: Path | None = None


def _find_amdgpu_device() -> Path | None:
    """amdgpu の DRM device ディレクトリ (/sys/class/drm/cardN/device) を探す。

    hwmon の name が amdgpu のカードを優先し、無ければ gpu_busy_percent の
    存在する最初のカードを返す。結果はキャッシュする(カード構成は再起動でしか変わらない)。
    """
    global _amdgpu_dev
    if _amdgpu_dev is not None and _amdgpu_dev.is_dir():
        return _amdgpu_dev
    fallback: Path | None = None
    for dev in sorted(Path("/sys/class/drm").glob("card[0-9]*/device")):
        if not (dev / "gpu_busy_percent").is_file():
            continue
        fallback = fallback or dev
        for h in dev.glob("hwmon/hwmon*"):
            try:
                if (h / "name").read_text().strip() == "amdgpu":
                    _amdgpu_dev = dev
                    return dev
            except OSError:
                continue
    _amdgpu_dev = fallback
    return fallback


def _read_sysfs_float(path: Path) -> float | None:
    try:
        return float(path.read_text().strip())
    except (OSError, ValueError):
        return None


def _amdgpu_hwmon(dev: Path) -> Path | None:
    for h in sorted(dev.glob("hwmon/hwmon*")):
        try:
            if (h / "name").read_text().strip() == "amdgpu":
                return h
        except OSError:
            continue
    return None


def _amdgpu_temp_c(h: Path) -> float | None:
    """GPU温度(℃)。label=junction のゾーンを優先し、無ければ計測値の最高値。"""
    best = None
    junction = None
    for t in sorted(h.glob("temp*_input")):
        v = _read_sysfs_float(t)
        if v is None:
            continue
        v /= 1000.0
        best = v if best is None else max(best, v)
        try:
            label = t.with_name(t.name.replace("_input", "_label")).read_text().strip().lower()
        except OSError:
            label = ""
        if label == "junction":
            junction = max(junction, v) if junction is not None else v
    return junction if junction is not None else best


def _amdgpu_clock_mhz(dev: Path) -> float | None:
    """現在の GPU クロック(MHz)。hwmon freq (label=sclk) 優先、pp_dpm_sclk の * 行にフォールバック。"""
    h = _amdgpu_hwmon(dev)
    if h is not None:
        for f in sorted(h.glob("freq*_input")):
            try:
                label = f.with_name(f.name.replace("_input", "_label")).read_text().strip().lower()
            except OSError:
                label = ""
            if label == "sclk":
                v = _read_sysfs_float(f)
                if v is not None:
                    return v / 1e6
    try:
        for ln in (dev / "pp_dpm_sclk").read_text().splitlines():
            m = re.match(r"^\s*\S+:\s*(\d+)Mhz\s*\*\s*$", ln)
            if m:
                return float(m.group(1))
    except OSError:
        pass
    return None


def _amdgpu_clock_max_mhz(dev: Path) -> float | None:
    """pp_dpm_sclk の数値付き行の最大値(ハード上限クロック)。"""
    vals = []
    try:
        for ln in (dev / "pp_dpm_sclk").read_text().splitlines():
            m = re.match(r"^\s*\d+:\s*(\d+)Mhz", ln)
            if m:
                vals.append(float(m.group(1)))
    except OSError:
        return None
    return max(vals) if vals else None


def _read_gpu_amdgpu() -> dict:
    """amdgpu sysfs で GPU メトリクスを取得する (WhitebearATOM2 / R9700)。

    rocm-smi はホストに未インストールのため /sys 直接読み。VRAM は
    mem_info_vram_used/total (ディスクリート 32GB。統合メモリではない)。
    """
    dev = _find_amdgpu_device()
    if dev is None:
        return {}
    out: dict = {"gpu_vendor": "amd"}
    busy = _read_sysfs_float(dev / "gpu_busy_percent")
    if busy is not None:
        out["gpu_load_pct"] = busy
    h = _amdgpu_hwmon(dev)
    if h is not None:
        t = _amdgpu_temp_c(h)
        if t is not None:
            out["gpu_temp_c"] = t
        pw = _read_sysfs_float(h / "power1_average")
        if pw is not None:
            out["gpu_power_w"] = pw / 1e6  # µW → W
        cap = _read_sysfs_float(h / "power1_cap")
        if cap:
            out["gpu_power_cap_w"] = cap / 1e6
        crit = _read_sysfs_float(h / "temp2_crit") or _read_sysfs_float(h / "temp1_crit")
        if crit:
            out["gpu_temp_crit_c"] = crit / 1000.0
    clk = _amdgpu_clock_mhz(dev)
    if clk is not None:
        out["gpu_clock_mhz"] = clk
    clkmax = _amdgpu_clock_max_mhz(dev)
    if clkmax:
        out["gpu_clock_max_mhz"] = clkmax
    vram_total = _read_sysfs_float(dev / "mem_info_vram_total")
    vram_used = _read_sysfs_float(dev / "mem_info_vram_used")
    if vram_total and vram_used is not None:
        out["vram_total_gib"] = vram_total / 2 ** 30
        out["vram_used_gib"] = vram_used / 2 ** 30
        out["vram_used_pct"] = vram_used / vram_total * 100.0
    gtt_total = _read_sysfs_float(dev / "mem_info_gtt_total")
    gtt_used = _read_sysfs_float(dev / "mem_info_gtt_used")
    if gtt_total and gtt_used is not None:
        out["gtt_used_gib"] = gtt_used / 2 ** 30
        out["gtt_total_gib"] = gtt_total / 2 ** 30
    return out


def _read_gpu() -> dict:
    """GPU メトリクス取得。バックエンドは config.GPU_BACKEND (nvidia|amdgpu)。"""
    if config.GPU_BACKEND == "amdgpu":
        return _read_gpu_amdgpu()
    return _read_gpu_nvidia()


def collect() -> dict:
    """ホストメトリクスのスナップショットを 1 回収集する。

    差分系(CPU負荷/ストレージ負荷)は前回の collect() 以降の値を返すため、
    初回呼び出しでは None になる。
    """
    now = time.time()
    out: dict = {
        "timestamp": now,
        "platform": config.PLATFORM,
        "memory_mode": config.MEMORY_MODE,
    }

    # --- CPU負荷 (/proc/stat idle 差分) ---
    idle, total = _read_cpu_times()
    if _prev["cpu_idle"] is not None and total > _prev["cpu_total"]:
        dt_idle = idle - _prev["cpu_idle"]
        dt_total = total - _prev["cpu_total"]
        out["cpu_load_pct"] = max(0.0, min(100.0, (1.0 - dt_idle / dt_total) * 100.0))
    else:
        out["cpu_load_pct"] = None
    _prev["cpu_idle"], _prev["cpu_total"] = idle, total

    # --- CPU温度 ---
    out["cpu_temp_c"] = _read_cpu_temp_c()
    out["cpu_cores"] = os.cpu_count() or 0

    # --- メモリ使用率 (dgx-spark=統合メモリ / atom2=RAM。VRAM は amdgpu 側で取得) ---
    info: dict[str, int] = {}
    with open("/proc/meminfo") as f:
        for line in f:
            k, v = line.split(":", 1)
            info[k.strip()] = int(v.strip().split()[0])  # kB
    mem_total = info.get("MemTotal", 0)
    mem_avail = info.get("MemAvailable", 0)
    out["mem_total_gib"] = mem_total / 1024 / 1024
    out["mem_used_gib"] = (mem_total - mem_avail) / 1024 / 1024
    out["mem_used_pct"] = (
        (mem_total - mem_avail) / mem_total * 100.0 if mem_total else None
    )

    # --- GPU (nvidia-smi / amdgpu sysfs) ---
    out.update(_read_gpu())

    # --- ストレージ使用率 ---
    du = shutil.disk_usage(_DISK_MOUNT)
    out["disk_total_gib"] = du.total / 2 ** 30
    out["disk_used_gib"] = du.used / 2 ** 30
    out["disk_used_pct"] = du.used / du.total * 100.0

    # --- ストレージ負荷 (/proc/diskstats 差分) ---
    d = _read_disk_stats()
    if d is not None:
        reads, writes, sr, sw, msr, msw = d
        pd = _prev["disk"]
        if pd is not None:
            dt = max(now - pd[0], 1e-9)
            ops = (reads - pd[1]) + (writes - pd[2])
            out["disk_read_mbps"] = (sr - pd[3]) * _SECTOR / dt / 1024 / 1024
            out["disk_write_mbps"] = (sw - pd[4]) * _SECTOR / dt / 1024 / 1024
            out["disk_iops"] = ops / dt
            out["disk_await_ms"] = ((msr - pd[5]) + (msw - pd[6])) / max(ops, 1)
        else:
            out["disk_read_mbps"] = None
            out["disk_write_mbps"] = None
            out["disk_iops"] = None
            out["disk_await_ms"] = None
        _prev["disk"] = (now, reads, writes, sr, sw, msr, msw)
    else:
        out["disk_read_mbps"] = None
        out["disk_write_mbps"] = None
        out["disk_iops"] = None
        out["disk_await_ms"] = None

    # --- ネットワーク負荷 (/proc/net/dev 差分, デフォルトルートIF) ---
    # Mbps = 10^6 bits/s。ゲージ上限(100%相当)は実リンク速度 (ethtool Speed 相当)
    iface = _default_iface()
    out["net_link_mbps"] = link_speed_mbps()
    out["net_max_mbps"] = out["net_link_mbps"] or int(config.NET_MAX_MBPS)
    n = _read_net_stats(iface) if iface else None
    if n is not None:
        rx, tx = n
        pn = _prev["net"]
        if pn is not None:
            dt = max(now - pn[0], 1e-9)
            out["net_down_mbps"] = max(0, rx - pn[1]) * 8 / dt / 1e6
            out["net_up_mbps"] = max(0, tx - pn[2]) * 8 / dt / 1e6
        else:
            out["net_down_mbps"] = None
            out["net_up_mbps"] = None
        _prev["net"] = (now, rx, tx)
    else:
        out["net_down_mbps"] = None
        out["net_up_mbps"] = None

    return out
