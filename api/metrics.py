"""ホストメトリクス収集(読み取り専用)。

データ源:
- CPU負荷: /proc/stat の idle 差分
- CPU温度: /sys/class/thermal/thermal_zone* (acpitz, ミリ℃)
- System Memory(統合メモリ): /proc/meminfo
- GPU負荷/温度/電力/クロック: nvidia-smi
- ストレージ使用率: shutil.disk_usage
- ストレージ負荷: /proc/diskstats 差分
"""

from __future__ import annotations

import os
import shutil
import subprocess
import time
from pathlib import Path

# 1 セクタ = 512 バイト
_SECTOR = 512

# ルートファイルシステム (nvme0n1p2) の親デバイス
_DISK_NAME = "nvme0n1"
_DISK_MOUNT = "/"

# 前回サンプル(差分計算用)
_prev: dict = {
    "cpu_idle": None,
    "cpu_total": None,
    "disk": None,  # (ts, reads, writes, sectors_read, sectors_written, ms_read, ms_write)
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


def _read_cpu_temp_c() -> float | None:
    """acpitz ゾーン全体の最高温度(℃)を返す。"""
    temps = []
    for z in sorted(Path("/sys/class/thermal").glob("thermal_zone*")):
        try:
            if (z / "type").read_text().strip() != "acpitz":
                continue
            temps.append(int((z / "temp").read_text()) / 1000.0)
        except (OSError, ValueError):
            continue
    return max(temps) if temps else None


def _read_gpu() -> dict:
    """nvidia-smi で GPU メトリクスを取得する。"""
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
            "gpu_load_pct": float(u),
            "gpu_temp_c": float(t),
            "gpu_power_w": float(p),
            "gpu_clock_mhz": float(c),
            "gpu_pstate": ps,
        }
    except (subprocess.SubprocessError, ValueError):
        return {}


def collect() -> dict:
    """ホストメトリクスのスナップショットを 1 回収集する。

    差分系(CPU負荷/ストレージ負荷)は前回の collect() 以降の値を返すため、
    初回呼び出しでは None になる。
    """
    now = time.time()
    out: dict = {"timestamp": now}

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

    # --- System Memory(統合メモリ使用率) ---
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

    # --- GPU (nvidia-smi) ---
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

    return out
