#!/usr/bin/env python3
"""Interactive local dashboard for monitoring computer resource usage.

This starts a small HTTP server on localhost and serves a live dashboard
showing CPU, memory, disk, network, and process activity. It only uses the
Python standard library, so it can run without extra packages.
"""

from __future__ import annotations

import argparse
import csv
import glob
import io
import json
import os
import platform
import re
import shutil
import socket
import subprocess
import threading
import time
from collections import deque
from dataclasses import dataclass, field
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Deque, Dict, List, Optional, Tuple
from urllib.parse import urlparse, parse_qs
import webbrowser


DEFAULT_SAMPLE_WINDOW = 120
DEFAULT_TOP_PROCESSES = 8
DEFAULT_PORT = 8765


def clamp(value: float, low: float = 0.0, high: float = 100.0) -> float:
    return max(low, min(high, value))


def human_bytes(value: float) -> str:
    units = ["B", "KB", "MB", "GB", "TB", "PB"]
    size = float(value)
    for unit in units:
        if size < 1024.0 or unit == units[-1]:
            return f"{size:.1f} {unit}"
        size /= 1024.0
    return f"{value:.1f} B"


# ---------------------------------------------------------------------------
# CPU: overall + per-core + iowait
# ---------------------------------------------------------------------------
def read_proc_stat() -> Dict[str, Any]:
    """Return overall (total, idle, iowait), per-core stats, and context switches."""
    total_stat: Tuple[int, int] = (0, 0)
    iowait_ticks = 0
    core_stats: Dict[str, Tuple[int, int]] = {}
    ctxt = 0
    procs_running = 0
    procs_blocked = 0

    with open("/proc/stat", "r", encoding="utf-8") as handle:
        for line in handle:
            parts = line.strip().split()
            if not parts:
                continue
            if parts[0] == "cpu":
                values = list(map(int, parts[1:]))
                idle = values[3] + (values[4] if len(values) > 4 else 0)
                total = sum(values)
                total_stat = (total, idle)
                iowait_ticks = values[4] if len(values) > 4 else 0
            elif parts[0].startswith("cpu"):
                core_id = parts[0]
                values = list(map(int, parts[1:]))
                idle = values[3] + (values[4] if len(values) > 4 else 0)
                total = sum(values)
                core_stats[core_id] = (total, idle)
            elif parts[0] == "ctxt":
                ctxt = int(parts[1])
            elif parts[0] == "procs_running":
                procs_running = int(parts[1])
            elif parts[0] == "procs_blocked":
                procs_blocked = int(parts[1])

    return {
        "total": total_stat[0],
        "idle": total_stat[1],
        "iowait_ticks": iowait_ticks,
        "cores": core_stats,
        "ctxt": ctxt,
        "procs_running": procs_running,
        "procs_blocked": procs_blocked,
    }


# ---------------------------------------------------------------------------
# CPU Temperature
# ---------------------------------------------------------------------------
def read_cpu_temperature() -> List[Dict[str, Any]]:
    """Read thermal zone temperatures from sysfs."""
    temps: List[Dict[str, Any]] = []
    thermal_base = "/sys/class/thermal"
    try:
        for zone_dir in sorted(glob.glob(os.path.join(thermal_base, "thermal_zone*"))):
            temp_file = os.path.join(zone_dir, "temp")
            type_file = os.path.join(zone_dir, "type")
            if not os.path.isfile(temp_file):
                continue
            try:
                with open(temp_file, "r", encoding="utf-8") as f:
                    raw = f.read().strip()
                temp_c = int(raw) / 1000.0
            except (ValueError, OSError):
                continue
            zone_type = "unknown"
            try:
                with open(type_file, "r", encoding="utf-8") as f:
                    zone_type = f.read().strip()
            except OSError:
                pass
            zone_name = os.path.basename(zone_dir)
            temps.append({"zone": zone_name, "type": zone_type, "temp_c": round(temp_c, 1)})
    except Exception:
        pass
    return temps


# ---------------------------------------------------------------------------
# Memory
# ---------------------------------------------------------------------------
def read_memory() -> Dict[str, float]:
    info: Dict[str, float] = {}
    with open("/proc/meminfo", "r", encoding="utf-8") as handle:
        for line in handle:
            key, raw_value = line.split(":", 1)
            parts = raw_value.strip().split()
            if parts:
                info[key] = float(parts[0]) * 1024.0

    total = info.get("MemTotal", 0.0)
    available = info.get("MemAvailable", info.get("MemFree", 0.0))
    used = max(0.0, total - available)
    swap_total = info.get("SwapTotal", 0.0)
    swap_free = info.get("SwapFree", 0.0)
    swap_used = max(0.0, swap_total - swap_free)
    buffers = info.get("Buffers", 0.0)
    cached = info.get("Cached", 0.0)
    slab = info.get("Slab", 0.0)
    return {
        "total": total,
        "available": available,
        "used": used,
        "percent": (used / total * 100.0) if total else 0.0,
        "swap_total": swap_total,
        "swap_used": swap_used,
        "swap_percent": (swap_used / swap_total * 100.0) if swap_total else 0.0,
        "buffers": buffers,
        "cached": cached,
        "slab": slab,
    }


def read_load() -> Dict[str, float]:
    try:
        one, five, fifteen = os.getloadavg()
    except OSError:
        one = five = fifteen = 0.0
    return {"1m": one, "5m": five, "15m": fifteen}


def read_uptime() -> float:
    try:
        with open("/proc/uptime", "r", encoding="utf-8") as handle:
            return float(handle.read().split()[0])
    except Exception:
        return 0.0


def read_disk(path: Path) -> Dict[str, float]:
    usage = shutil.disk_usage(path)
    used = usage.total - usage.free
    return {
        "path": str(path),
        "total": float(usage.total),
        "used": float(used),
        "free": float(usage.free),
        "percent": (used / usage.total * 100.0) if usage.total else 0.0,
    }


# ---------------------------------------------------------------------------
# Disk I/O
# ---------------------------------------------------------------------------
def read_disk_io() -> Dict[str, int]:
    """Read aggregate disk read/write bytes from /proc/diskstats."""
    read_sectors = 0
    write_sectors = 0
    try:
        with open("/proc/diskstats", "r", encoding="utf-8") as handle:
            for line in handle:
                parts = line.split()
                if len(parts) < 14:
                    continue
                dev_name = parts[2]
                # Only count whole-disk devices, skip partitions
                if re.match(r"^(sd[a-z]+|nvme\d+n\d+|vd[a-z]+|xvd[a-z]+)$", dev_name):
                    read_sectors += int(parts[5])
                    write_sectors += int(parts[9])
    except Exception:
        pass
    # Each sector is typically 512 bytes
    return {"read_bytes": read_sectors * 512, "write_bytes": write_sectors * 512}


# ---------------------------------------------------------------------------
# Network
# ---------------------------------------------------------------------------
def read_network_totals() -> Dict[str, int]:
    totals = {"rx": 0, "tx": 0}
    try:
        with open("/proc/net/dev", "r", encoding="utf-8") as handle:
            for line in handle.readlines()[2:]:
                if ":" not in line:
                    continue
                _, payload = line.split(":", 1)
                parts = payload.split()
                if len(parts) >= 16:
                    totals["rx"] += int(parts[0])
                    totals["tx"] += int(parts[8])
    except Exception:
        pass
    return totals


# ---------------------------------------------------------------------------
# GPU
# ---------------------------------------------------------------------------
def read_gpu_info() -> List[Dict[str, str]]:
    command = [
        "nvidia-smi",
        "--query-gpu=index,name,utilization.gpu,utilization.memory,memory.used,memory.total,temperature.gpu,power.draw,power.limit,fan.speed,clocks.current.sm,clocks.max.sm",
        "--format=csv,noheader,nounits",
    ]
    try:
        output = subprocess.check_output(command, stderr=subprocess.DEVNULL, text=True)
    except Exception:
        return []

    gpus: List[Dict[str, str]] = []
    for raw_line in output.strip().splitlines():
        parts = [part.strip() for part in raw_line.split(",")]
        if len(parts) >= 6:
            gpus.append(
                {
                    "index": parts[0],
                    "name": parts[1],
                    "utilization": parts[2],
                    "memory_utilization": parts[3],
                    "memory_used": parts[4],
                    "memory_total": parts[5],
                    "temperature": parts[6] if len(parts) > 6 else "N/A",
                    "power_draw": parts[7] if len(parts) > 7 else "N/A",
                    "power_limit": parts[8] if len(parts) > 8 else "N/A",
                    "fan_speed": parts[9] if len(parts) > 9 else "N/A",
                    "clock_sm": parts[10] if len(parts) > 10 else "N/A",
                    "clock_sm_max": parts[11] if len(parts) > 11 else "N/A",
                }
            )
    return gpus


def read_gpu_processes() -> List[Dict[str, str]]:
    """Return list of processes running on GPUs via nvidia-smi."""
    command = [
        "nvidia-smi",
        "--query-compute-apps=pid,process_name,used_gpu_memory,gpu_bus_id",
        "--format=csv,noheader,nounits",
    ]
    try:
        output = subprocess.check_output(command, stderr=subprocess.DEVNULL, text=True)
    except Exception:
        return []

    processes: List[Dict[str, str]] = []
    for raw_line in output.strip().splitlines():
        parts = [p.strip() for p in raw_line.split(",")]
        if len(parts) >= 3:
            processes.append(
                {
                    "pid": parts[0],
                    "name": parts[1],
                    "gpu_mem_mb": parts[2],
                    "bus_id": parts[3] if len(parts) > 3 else "",
                }
            )
    return processes


# ---------------------------------------------------------------------------
# System Processes
# ---------------------------------------------------------------------------
def read_top_processes(limit: int) -> List[Dict[str, str]]:
    command = [
        "ps",
        "-eo",
        "pid,comm,%cpu,%mem,rss,stat",
        "--sort=-%cpu",
    ]
    try:
        output = subprocess.check_output(command, text=True)
    except Exception:
        return []

    processes: List[Dict[str, str]] = []
    lines = output.strip().splitlines()[1 : limit + 1]
    for line in lines:
        parts = line.split(None, 5)
        if len(parts) >= 5:
            pid, command_name, cpu, mem, rss = parts[0], parts[1], parts[2], parts[3], parts[4]
            stat = parts[5] if len(parts) > 5 else ""
            processes.append(
                {
                    "pid": pid,
                    "name": command_name,
                    "cpu": cpu,
                    "mem": mem,
                    "rss": human_bytes(int(rss) * 1024),
                    "stat": stat,
                }
            )
    return processes


def count_zombie_processes() -> int:
    """Count processes in zombie (defunct) state."""
    try:
        output = subprocess.check_output(
            ["ps", "-eo", "stat"], text=True, stderr=subprocess.DEVNULL
        )
        return sum(1 for line in output.strip().splitlines()[1:] if line.strip().startswith("Z"))
    except Exception:
        return 0


# ---------------------------------------------------------------------------
# State
# ---------------------------------------------------------------------------
@dataclass
class ResourceState:
    cpu_history: Deque[float] = field(default_factory=lambda: deque(maxlen=DEFAULT_SAMPLE_WINDOW))
    mem_history: Deque[float] = field(default_factory=lambda: deque(maxlen=DEFAULT_SAMPLE_WINDOW))
    net_rx_history: Deque[float] = field(default_factory=lambda: deque(maxlen=DEFAULT_SAMPLE_WINDOW))
    net_tx_history: Deque[float] = field(default_factory=lambda: deque(maxlen=DEFAULT_SAMPLE_WINDOW))
    gpu_util_history: Deque[float] = field(default_factory=lambda: deque(maxlen=DEFAULT_SAMPLE_WINDOW))
    gpu_mem_history: Deque[float] = field(default_factory=lambda: deque(maxlen=DEFAULT_SAMPLE_WINDOW))
    gpu_temp_history: Deque[float] = field(default_factory=lambda: deque(maxlen=DEFAULT_SAMPLE_WINDOW))
    gpu_power_history: Deque[float] = field(default_factory=lambda: deque(maxlen=DEFAULT_SAMPLE_WINDOW))
    disk_read_history: Deque[float] = field(default_factory=lambda: deque(maxlen=DEFAULT_SAMPLE_WINDOW))
    disk_write_history: Deque[float] = field(default_factory=lambda: deque(maxlen=DEFAULT_SAMPLE_WINDOW))
    iowait_history: Deque[float] = field(default_factory=lambda: deque(maxlen=DEFAULT_SAMPLE_WINDOW))
    # Snapshots for diff-based rates
    last_cpu_total: Optional[int] = None
    last_cpu_idle: Optional[int] = None
    last_cpu_iowait: Optional[int] = None
    last_cpu_cores: Optional[Dict[str, Tuple[int, int]]] = None
    last_net_totals: Optional[Dict[str, int]] = None
    last_disk_io: Optional[Dict[str, int]] = None
    last_ctxt: Optional[int] = None
    last_sample_time: Optional[float] = None
    # CSV logging
    csv_rows: List[Dict[str, Any]] = field(default_factory=list)


STATE = ResourceState()
STATE_LOCK = threading.Lock()


def sample_metrics(sample_root: Path, top_limit: int) -> Dict[str, object]:
    now = time.time()
    cpu_info = read_proc_stat()
    cpu_total = cpu_info["total"]
    cpu_idle = cpu_info["idle"]
    cpu_iowait = cpu_info["iowait_ticks"]
    cpu_cores = cpu_info["cores"]
    memory = read_memory()
    disk = read_disk(sample_root)
    disk_io = read_disk_io()
    load = read_load()
    uptime = read_uptime()
    network = read_network_totals()
    gpu = read_gpu_info()
    gpu_procs = read_gpu_processes()
    processes = read_top_processes(top_limit)
    cpu_temps = read_cpu_temperature()
    zombies = count_zombie_processes()

    with STATE_LOCK:
        # ---- CPU overall ----
        if STATE.last_cpu_total is None or STATE.last_cpu_idle is None:
            cpu_percent = 0.0
            iowait_percent = 0.0
        else:
            delta_total = cpu_total - STATE.last_cpu_total
            delta_idle = cpu_idle - STATE.last_cpu_idle
            delta_iowait = cpu_iowait - (STATE.last_cpu_iowait or 0)
            cpu_percent = 0.0
            iowait_percent = 0.0
            if delta_total > 0:
                cpu_percent = clamp((1.0 - (delta_idle / delta_total)) * 100.0)
                iowait_percent = clamp((delta_iowait / delta_total) * 100.0)

        # ---- Network rates ----
        if STATE.last_net_totals is None or STATE.last_sample_time is None:
            rx_rate = 0.0
            tx_rate = 0.0
        else:
            elapsed = max(0.001, now - STATE.last_sample_time)
            rx_rate = max(0.0, (network["rx"] - STATE.last_net_totals["rx"]) / elapsed)
            tx_rate = max(0.0, (network["tx"] - STATE.last_net_totals["tx"]) / elapsed)

        # ---- Disk I/O rates ----
        if STATE.last_disk_io is None or STATE.last_sample_time is None:
            disk_read_rate = 0.0
            disk_write_rate = 0.0
        else:
            elapsed = max(0.001, now - STATE.last_sample_time)
            disk_read_rate = max(0.0, (disk_io["read_bytes"] - STATE.last_disk_io["read_bytes"]) / elapsed)
            disk_write_rate = max(0.0, (disk_io["write_bytes"] - STATE.last_disk_io["write_bytes"]) / elapsed)

        # ---- Context switch rate ----
        ctxt_rate = 0.0
        if STATE.last_ctxt is not None and STATE.last_sample_time is not None:
            elapsed = max(0.001, now - STATE.last_sample_time)
            ctxt_rate = max(0.0, (cpu_info["ctxt"] - STATE.last_ctxt) / elapsed)

        # ---- Per-core ----
        core_percents: Dict[str, float] = {}
        if STATE.last_cpu_cores:
            for core, (c_total, c_idle) in cpu_cores.items():
                if core in STATE.last_cpu_cores:
                    prev_total, prev_idle = STATE.last_cpu_cores[core]
                    d_total = c_total - prev_total
                    d_idle = c_idle - prev_idle
                    if d_total > 0:
                        core_percents[core] = clamp((1.0 - (d_idle / d_total)) * 100.0)

        # ---- GPU aggregates for history ----
        gpu_util_avg = 0.0
        gpu_mem_avg = 0.0
        gpu_temp_max = 0.0
        gpu_power_sum = 0.0
        if gpu:
            utils = [float(g["utilization"]) for g in gpu if g["utilization"] not in ("N/A", "[N/A]", "")]
            mems = [float(g["memory_utilization"]) for g in gpu if g["memory_utilization"] not in ("N/A", "[N/A]", "")]
            tmps = [float(g["temperature"]) for g in gpu if g["temperature"] not in ("N/A", "[N/A]", "")]
            pwrs = [float(g["power_draw"]) for g in gpu if g["power_draw"] not in ("N/A", "[N/A]", "")]
            if utils:
                gpu_util_avg = sum(utils) / len(utils)
            if mems:
                gpu_mem_avg = sum(mems) / len(mems)
            if tmps:
                gpu_temp_max = max(tmps)
            if pwrs:
                gpu_power_sum = sum(pwrs)

        # ---- Update state snapshots ----
        STATE.last_cpu_total = cpu_total
        STATE.last_cpu_idle = cpu_idle
        STATE.last_cpu_iowait = cpu_iowait
        STATE.last_cpu_cores = cpu_cores
        STATE.last_net_totals = network
        STATE.last_disk_io = disk_io
        STATE.last_ctxt = cpu_info["ctxt"]
        STATE.last_sample_time = now

        STATE.cpu_history.append(cpu_percent)
        STATE.mem_history.append(memory["percent"])
        STATE.net_rx_history.append(rx_rate)
        STATE.net_tx_history.append(tx_rate)
        STATE.gpu_util_history.append(gpu_util_avg)
        STATE.gpu_mem_history.append(gpu_mem_avg)
        STATE.gpu_temp_history.append(gpu_temp_max)
        STATE.gpu_power_history.append(gpu_power_sum)
        STATE.disk_read_history.append(disk_read_rate)
        STATE.disk_write_history.append(disk_write_rate)
        STATE.iowait_history.append(iowait_percent)

        cpu_history = list(STATE.cpu_history)
        mem_history = list(STATE.mem_history)
        rx_history = list(STATE.net_rx_history)
        tx_history = list(STATE.net_tx_history)
        gpu_util_hist = list(STATE.gpu_util_history)
        gpu_mem_hist = list(STATE.gpu_mem_history)
        gpu_temp_hist = list(STATE.gpu_temp_history)
        gpu_power_hist = list(STATE.gpu_power_history)
        disk_read_hist = list(STATE.disk_read_history)
        disk_write_hist = list(STATE.disk_write_history)
        iowait_hist = list(STATE.iowait_history)

        # ---- Store row for CSV export ----
        csv_row = {
            "timestamp": now,
            "cpu_percent": round(cpu_percent, 2),
            "iowait_percent": round(iowait_percent, 2),
            "mem_percent": round(memory["percent"], 2),
            "mem_used_bytes": memory["used"],
            "swap_percent": round(memory["swap_percent"], 2),
            "gpu_util_avg": round(gpu_util_avg, 2),
            "gpu_mem_avg": round(gpu_mem_avg, 2),
            "gpu_temp_max": round(gpu_temp_max, 1),
            "gpu_power_w": round(gpu_power_sum, 1),
            "disk_read_rate": round(disk_read_rate, 0),
            "disk_write_rate": round(disk_write_rate, 0),
            "net_rx_rate": round(rx_rate, 0),
            "net_tx_rate": round(tx_rate, 0),
        }
        STATE.csv_rows.append(csv_row)
        # Keep last 10000 rows (~5.5 hours at 2s interval)
        if len(STATE.csv_rows) > 10000:
            STATE.csv_rows = STATE.csv_rows[-10000:]

    return {
        "timestamp": now,
        "host": socket.gethostname(),
        "platform": platform.platform(),
        "uptime_seconds": uptime,
        "load_average": load,
        "cpu_percent": cpu_percent,
        "iowait_percent": iowait_percent,
        "cpu_cores": core_percents,
        "cpu_history": cpu_history,
        "iowait_history": iowait_hist,
        "cpu_temps": cpu_temps,
        "ctxt_rate": ctxt_rate,
        "procs_running": cpu_info["procs_running"],
        "procs_blocked": cpu_info["procs_blocked"],
        "zombies": zombies,
        "memory": memory,
        "mem_history": mem_history,
        "disk": disk,
        "disk_io": {
            "read_rate": disk_read_rate,
            "write_rate": disk_write_rate,
            "read_history": disk_read_hist,
            "write_history": disk_write_hist,
        },
        "network": {
            "rx_total": network["rx"],
            "tx_total": network["tx"],
            "rx_rate": rx_rate,
            "tx_rate": tx_rate,
            "rx_history": rx_history,
            "tx_history": tx_history,
        },
        "gpu": gpu,
        "gpu_processes": gpu_procs,
        "gpu_history": {
            "util": gpu_util_hist,
            "mem": gpu_mem_hist,
            "temp": gpu_temp_hist,
            "power": gpu_power_hist,
        },
        "processes": processes,
        "active_user": os.environ.get("USER", "unknown"),
    }


def generate_csv() -> str:
    """Generate CSV string from stored metric rows."""
    with STATE_LOCK:
        rows = list(STATE.csv_rows)
    if not rows:
        return "No data collected yet.\n"
    output = io.StringIO()
    fieldnames = list(rows[0].keys())
    writer = csv.DictWriter(output, fieldnames=fieldnames)
    writer.writeheader()
    writer.writerows(rows)
    return output.getvalue()


# ---------------------------------------------------------------------------
# HTML Dashboard
# ---------------------------------------------------------------------------
HTML_TEMPLATE = """<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8" />
  <meta name="viewport" content="width=device-width, initial-scale=1" />
  <title>Resource Dashboard – AIIMS Rishikesh</title>
  <link rel="preconnect" href="https://fonts.googleapis.com">
  <link href="https://fonts.googleapis.com/css2?family=Inter:wght@400;500;600;700;800&display=swap" rel="stylesheet">
  <style>
    :root {
      color-scheme: dark;
      --bg: #0b1020;
      --panel: rgba(15, 23, 42, 0.88);
      --panel-border: rgba(148, 163, 184, 0.18);
      --text: #e5eefc;
      --muted: #92a2bf;
      --accent: #5eead4;
      --accent-2: #60a5fa;
      --warn: #fbbf24;
      --danger: #fb7185;
      --good: #4ade80;
    }

    * { box-sizing: border-box; }
    body {
      margin: 0;
      font-family: 'Inter', ui-sans-serif, system-ui, -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif;
      background:
        radial-gradient(circle at top left, rgba(96, 165, 250, 0.18), transparent 28%),
        radial-gradient(circle at 80% 10%, rgba(94, 234, 212, 0.12), transparent 24%),
        linear-gradient(180deg, #070b16 0%, #0b1020 56%, #111827 100%);
      color: var(--text);
      min-height: 100vh;
    }

    .wrap { max-width: 1520px; margin: 0 auto; padding: 24px; }

    /* --- Hero --- */
    .hero {
      display: grid;
      grid-template-columns: 1.3fr 0.9fr;
      gap: 16px;
      margin-bottom: 16px;
    }
    .title-card, .stats-card, .panel {
      background: var(--panel);
      border: 1px solid var(--panel-border);
      border-radius: 20px;
      box-shadow: 0 18px 45px rgba(0,0,0,0.28);
      backdrop-filter: blur(12px);
    }
    .title-card { padding: 24px; }
    h1 { margin: 0 0 10px; font-size: clamp(1.8rem, 3.5vw, 3rem); letter-spacing: -0.03em; font-weight: 800; }
    .subtitle { margin: 0; color: var(--muted); max-width: 70ch; line-height: 1.55; }
    .meta { display: flex; flex-wrap: wrap; gap: 10px; margin-top: 16px; color: var(--muted); font-size: 0.9rem; }
    .pill { padding: 6px 12px; border-radius: 999px; background: rgba(96, 165, 250, 0.12); border: 1px solid rgba(96, 165, 250, 0.25); }
    .stats-card { padding: 16px 20px; display: grid; gap: 8px; align-content: center; }
    .stat-row { display: grid; grid-template-columns: 130px 1fr auto; gap: 10px; align-items: center; }
    .label { color: var(--muted); font-size: 0.88rem; }
    .value { font-weight: 700; font-size: 0.95rem; white-space: nowrap; }

    /* --- Bars --- */
    .bar {
      height: 12px; border-radius: 999px; overflow: hidden;
      background: rgba(148, 163, 184, 0.16);
      border: 1px solid rgba(148, 163, 184, 0.12);
    }
    .fill { height: 100%; width: 0%; border-radius: inherit; transition: width 0.35s ease; }

    /* --- Grid layout --- */
    .grid { display: grid; grid-template-columns: repeat(12, 1fr); gap: 14px; margin-bottom: 14px; }
    .panel { padding: 16px; }
    .span-3 { grid-column: span 3; }
    .span-4 { grid-column: span 4; }
    .span-6 { grid-column: span 6; }
    .span-8 { grid-column: span 8; }
    .span-12 { grid-column: span 12; }
    .panel h2 { margin: 0 0 10px; font-size: 1rem; letter-spacing: 0.02em; font-weight: 700; }
    .chart { width: 100%; height: 120px; }
    .chart-line { fill: none; stroke-width: 2; }
    .chart-area { fill-opacity: 0.18; }
    .section-head { display: flex; justify-content: space-between; align-items: baseline; gap: 12px; margin-bottom: 8px; }
    .tiny { color: var(--muted); font-size: 0.82rem; }

    /* --- Tables --- */
    table { width: 100%; border-collapse: collapse; }
    th, td { text-align: left; padding: 8px 6px; border-bottom: 1px solid rgba(148, 163, 184, 0.14); font-size: 0.88rem; }
    th { color: var(--muted); font-weight: 600; font-size: 0.78rem; text-transform: uppercase; letter-spacing: 0.08em; }

    .grid-two { display: grid; grid-template-columns: 1fr 1fr; gap: 14px; }
    .gpu-card { display: grid; gap: 8px; }
    .gpu-item { padding: 10px; border-radius: 14px; background: rgba(148, 163, 184, 0.08); border: 1px solid rgba(148, 163, 184, 0.12); }

    /* --- Alert flashing --- */
    @keyframes alert-pulse {
      0%, 100% { box-shadow: 0 0 0 0 rgba(251, 113, 133, 0); }
      50% { box-shadow: 0 0 20px 4px rgba(251, 113, 133, 0.5); }
    }
    .alert-active { animation: alert-pulse 1.4s ease-in-out infinite; border-color: var(--danger) !important; }

    /* --- Heatmap cells --- */
    .heatmap-grid { display: flex; flex-wrap: wrap; gap: 3px; margin-top: 8px; }
    .heatmap-cell {
      width: 28px; height: 28px; border-radius: 6px; display: flex; align-items: center; justify-content: center;
      font-size: 0.65rem; font-weight: 600; color: #fff; transition: background 0.3s ease;
    }

    /* --- Training timer --- */
    .timer-box {
      display: flex; align-items: center; gap: 10px; margin-top: 12px;
    }
    .timer-btn {
      padding: 6px 16px; border-radius: 10px; border: 1px solid var(--panel-border);
      background: rgba(94, 234, 212, 0.15); color: var(--accent); cursor: pointer;
      font-size: 0.85rem; font-weight: 600; transition: background 0.2s;
    }
    .timer-btn:hover { background: rgba(94, 234, 212, 0.3); }
    .timer-btn.stop { background: rgba(251, 113, 133, 0.15); color: var(--danger); }
    .timer-btn.stop:hover { background: rgba(251, 113, 133, 0.3); }
    .timer-display { font-size: 1.6rem; font-weight: 800; font-variant-numeric: tabular-nums; letter-spacing: 0.03em; }

    /* --- Toolbar --- */
    .toolbar {
      display: flex; flex-wrap: wrap; gap: 10px; align-items: center; margin-bottom: 16px;
      padding: 10px 16px; border-radius: 14px;
      background: rgba(15, 23, 42, 0.6); border: 1px solid var(--panel-border);
    }
    .toolbar label { font-size: 0.85rem; color: var(--muted); }
    .toolbar select, .toolbar input[type=range] {
      background: rgba(148,163,184,0.12); border: 1px solid var(--panel-border);
      color: var(--text); border-radius: 8px; padding: 4px 8px; font-size: 0.85rem;
    }
    .export-btn {
      margin-left: auto;
      padding: 6px 16px; border-radius: 10px; border: 1px solid rgba(96, 165, 250, 0.35);
      background: rgba(96, 165, 250, 0.12); color: var(--accent-2); cursor: pointer;
      font-size: 0.85rem; font-weight: 600; text-decoration: none; transition: background 0.2s;
    }
    .export-btn:hover { background: rgba(96, 165, 250, 0.3); }

    .footer { margin-top: 14px; color: var(--muted); font-size: 0.84rem; display: flex; justify-content: space-between; flex-wrap: wrap; gap: 8px; }

    @media (max-width: 1100px) {
      .hero, .grid-two { grid-template-columns: 1fr; }
      .span-3, .span-4, .span-6, .span-8 { grid-column: span 12; }
    }
  </style>
</head>
<body>
  <div class="wrap">

    <!-- Toolbar -->
    <div class="toolbar">
      <label>Refresh: <span id="refresh-val">2</span>s</label>
      <input type="range" id="refresh-slider" min="1" max="10" value="2" step="1" />
      <label style="margin-left: 12px;">🔔 Alerts</label>
      <select id="alert-toggle">
        <option value="on">On</option>
        <option value="off">Off</option>
      </select>
      <label style="margin-left: 12px;">🔕 Notifications</label>
      <select id="notif-toggle">
        <option value="off">Off</option>
        <option value="on">On</option>
      </select>
      <a class="export-btn" href="/api/export_csv" download="resource_metrics.csv">📥 Export CSV</a>
    </div>

    <!-- Hero -->
    <div class="hero">
      <div class="title-card">
        <h1>🖥️ Resource Dashboard</h1>
        <p class="subtitle">Live local monitor for CPU, memory, disk I/O, network, GPU, and training workloads. Designed for the AIIMS Rishikesh burn image generation pipeline.</p>
        <div class="meta">
          <span class="pill" id="host-pill">Host: loading...</span>
          <span class="pill" id="platform-pill">Platform: loading...</span>
          <span class="pill" id="uptime-pill">Uptime: loading...</span>
          <span class="pill" id="zombie-pill" style="display:none;">🧟 Zombies: 0</span>
        </div>
        <!-- Training timer -->
        <div class="timer-box">
          <button class="timer-btn" id="timer-toggle" onclick="toggleTimer()">▶ Start Timer</button>
          <button class="timer-btn stop" id="timer-reset" onclick="resetTimer()">↺ Reset</button>
          <div class="timer-display" id="timer-display">00:00:00</div>
        </div>
      </div>
      <div class="stats-card" id="stats-card">
        <div class="stat-row"><div class="label">CPU</div><div class="bar"><div class="fill" id="cpu-fill" style="background: linear-gradient(90deg, var(--accent), var(--accent-2));"></div></div><div class="value" id="cpu-value">0%</div></div>
        <div class="stat-row"><div class="label">IO-Wait</div><div class="bar"><div class="fill" id="iowait-fill" style="background: linear-gradient(90deg, #f97316, #ef4444);"></div></div><div class="value" id="iowait-value">0%</div></div>
        <div class="stat-row"><div class="label">Memory</div><div class="bar"><div class="fill" id="mem-fill" style="background: linear-gradient(90deg, var(--warn), #f97316);"></div></div><div class="value" id="mem-value">0%</div></div>
        <div class="stat-row"><div class="label">GPU Core</div><div class="bar"><div class="fill" id="gpu-core-fill" style="background: linear-gradient(90deg, #22d3ee, #3b82f6);"></div></div><div class="value" id="gpu-core-value">N/A</div></div>
        <div class="stat-row"><div class="label">GPU Memory</div><div class="bar"><div class="fill" id="gpu-mem-fill" style="background: linear-gradient(90deg, #34d399, #10b981);"></div></div><div class="value" id="gpu-mem-value">N/A</div></div>
        <div class="stat-row"><div class="label">Disk</div><div class="bar"><div class="fill" id="disk-fill" style="background: linear-gradient(90deg, #a78bfa, #60a5fa);"></div></div><div class="value" id="disk-value">0%</div></div>
        <div class="stat-row"><div class="label">Network</div><div class="bar"><div class="fill" id="net-fill" style="background: linear-gradient(90deg, var(--good), var(--accent));"></div></div><div class="value" id="net-value">0 B/s</div></div>
      </div>
    </div>

    <!-- Row 1: CPU Trend + IO-Wait, Memory Trend -->
    <div class="grid">
      <div class="panel span-6" id="panel-cpu">
        <div class="section-head"><h2>CPU Trend</h2><span class="tiny">overall + io-wait</span></div>
        <svg class="chart" id="cpu-chart" viewBox="0 0 600 120" preserveAspectRatio="none"></svg>
        <div class="section-head" style="margin-top:10px;"><h2>Per-Core Heatmap</h2><span class="tiny" id="core-count"></span></div>
        <div class="heatmap-grid" id="cpu-heatmap"></div>
      </div>
      <div class="panel span-6" id="panel-mem">
        <div class="section-head"><h2>Memory Trend</h2><span class="tiny">used %</span></div>
        <svg class="chart" id="mem-chart" viewBox="0 0 600 120" preserveAspectRatio="none"></svg>
        <div id="mem-details" style="display: flex; flex-wrap: wrap; gap: 8px; margin-top: 10px; font-size: 0.82rem; color: var(--muted);"></div>
      </div>
    </div>

    <!-- Row 2: GPU Util Trend, GPU Mem / Temp / Power Trend -->
    <div class="grid">
      <div class="panel span-6">
        <div class="section-head"><h2>GPU Utilization Trend</h2><span class="tiny">core % avg</span></div>
        <svg class="chart" id="gpu-util-chart" viewBox="0 0 600 120" preserveAspectRatio="none"></svg>
      </div>
      <div class="panel span-6">
        <div class="section-head"><h2>GPU Memory / Temp / Power</h2><span class="tiny">history</span></div>
        <svg class="chart" id="gpu-extra-chart" viewBox="0 0 600 120" preserveAspectRatio="none"></svg>
        <div style="display: flex; gap: 14px; margin-top: 6px; font-size: 0.78rem; color: var(--muted);">
          <span>🟢 Mem%</span><span>🟠 Temp°C</span><span>🔵 Power W</span>
        </div>
      </div>
    </div>

    <!-- Row 3: Network, Disk I/O -->
    <div class="grid">
      <div class="panel span-6">
        <div class="section-head"><h2>Network Trend</h2><span class="tiny">rx / tx rates</span></div>
        <svg class="chart" id="net-chart" viewBox="0 0 600 120" preserveAspectRatio="none"></svg>
      </div>
      <div class="panel span-6">
        <div class="section-head"><h2>Disk I/O Trend</h2><span class="tiny">read / write throughput</span></div>
        <svg class="chart" id="diskio-chart" viewBox="0 0 600 120" preserveAspectRatio="none"></svg>
        <div style="display: flex; gap: 14px; margin-top: 6px; font-size: 0.78rem; color: var(--muted);">
          <span>🟢 Read</span><span>🔵 Write</span>
          <span id="diskio-rates" style="margin-left:auto;"></span>
        </div>
      </div>
    </div>

    <!-- Row 4: Disk/Load/Temp, Processes, GPU Cards -->
    <div class="grid">
      <div class="panel span-3">
        <h2>System Info</h2>
        <div class="gpu-card" id="sysinfo-box"></div>
      </div>
      <div class="panel span-5">
        <div class="section-head"><h2>Top Processes</h2><span class="tiny">sorted by CPU</span></div>
        <table>
          <thead><tr><th>PID</th><th>Command</th><th>CPU%</th><th>MEM%</th><th>RSS</th><th>Stat</th></tr></thead>
          <tbody id="process-body"></tbody>
        </table>
      </div>
      <div class="panel span-4" id="panel-gpu">
        <div class="section-head"><h2>GPU Cards</h2><span class="tiny">nvidia-smi</span></div>
        <div class="gpu-card" id="gpu-box"></div>
        <div style="margin-top: 12px;">
          <div class="section-head"><h2>GPU Processes</h2><span class="tiny">active compute</span></div>
          <div class="gpu-card" id="gpu-proc-box"></div>
        </div>
      </div>
    </div>

    <div class="footer">
      <span id="footer">Loading...</span>
      <span id="alert-status"></span>
    </div>

  </div>

  <script>
    // ---- State ----
    const historyLimit = 120;
    const cpuHistory = [];
    const memHistory = [];
    const netHistory = [];
    const gpuUtilHistory = [];
    const gpuMemHistory = [];
    const gpuTempHistory = [];
    const gpuPowerHistory = [];
    const diskReadHistory = [];
    const diskWriteHistory = [];
    const iowaitHistory = [];
    let refreshInterval = 2000;
    let refreshTimer = null;
    let lastGpuWasActive = null;

    // ---- Training timer ----
    let timerRunning = false;
    let timerStart = null;
    let timerElapsed = 0;
    let timerAnimFrame = null;

    (function restoreTimer() {
      const saved = localStorage.getItem('training_timer');
      if (saved) {
        const d = JSON.parse(saved);
        timerElapsed = d.elapsed || 0;
        if (d.running && d.start) {
          timerRunning = true;
          timerStart = d.start;
          timerElapsed = d.elapsed;
          document.addEventListener('DOMContentLoaded', () => {
            document.getElementById('timer-toggle').textContent = '⏸ Pause';
            tickTimer();
          });
        }
      }
    })();

    function toggleTimer() {
      if (timerRunning) {
        timerElapsed += (Date.now() - timerStart);
        timerRunning = false;
        timerStart = null;
        document.getElementById('timer-toggle').textContent = '▶ Resume';
        cancelAnimationFrame(timerAnimFrame);
      } else {
        timerRunning = true;
        timerStart = Date.now();
        document.getElementById('timer-toggle').textContent = '⏸ Pause';
        tickTimer();
      }
      saveTimer();
    }
    function resetTimer() {
      timerRunning = false;
      timerStart = null;
      timerElapsed = 0;
      document.getElementById('timer-toggle').textContent = '▶ Start Timer';
      document.getElementById('timer-display').textContent = '00:00:00';
      cancelAnimationFrame(timerAnimFrame);
      saveTimer();
    }
    function tickTimer() {
      if (!timerRunning) return;
      const total = timerElapsed + (Date.now() - timerStart);
      const secs = Math.floor(total / 1000);
      const h = String(Math.floor(secs / 3600)).padStart(2, '0');
      const m = String(Math.floor((secs % 3600) / 60)).padStart(2, '0');
      const s = String(secs % 60).padStart(2, '0');
      document.getElementById('timer-display').textContent = `${h}:${m}:${s}`;
      timerAnimFrame = requestAnimationFrame(tickTimer);
    }
    function saveTimer() {
      localStorage.setItem('training_timer', JSON.stringify({
        running: timerRunning, start: timerStart, elapsed: timerElapsed
      }));
    }

    // ---- Refresh interval slider ----
    document.addEventListener('DOMContentLoaded', () => {
      const slider = document.getElementById('refresh-slider');
      slider.addEventListener('input', () => {
        const val = parseInt(slider.value);
        document.getElementById('refresh-val').textContent = val;
        refreshInterval = val * 1000;
        clearInterval(refreshTimer);
        refreshTimer = setInterval(refresh, refreshInterval);
      });
    });

    // ---- Helpers ----
    function formatBytes(value) {
      const units = ['B', 'KB', 'MB', 'GB', 'TB', 'PB'];
      let size = Math.max(0, value);
      let idx = 0;
      while (size >= 1024 && idx < units.length - 1) { size /= 1024; idx++; }
      return `${size.toFixed(1)} ${units[idx]}`;
    }
    function formatDuration(seconds) {
      const total = Math.max(0, Math.floor(seconds));
      const d = Math.floor(total / 86400), h = Math.floor((total % 86400) / 3600);
      const m = Math.floor((total % 3600) / 60), s = total % 60;
      if (d > 0) return `${d}d ${h}h ${m}m`;
      if (h > 0) return `${h}h ${m}m ${s}s`;
      if (m > 0) return `${m}m ${s}s`;
      return `${s}s`;
    }
    function pushHistory(target, value) {
      target.push(value);
      if (target.length > historyLimit) target.shift();
    }

    // ---- Chart rendering ----
    function renderChart(svgId, datasets) {
      const svg = document.getElementById(svgId);
      if (!svg) return;
      const width = 600, height = 120;
      let html = '';
      datasets.forEach(({values, color, fillColor}, di) => {
        if (!values.length) return;
        const max = Math.max(100, ...values, 1);
        const stepX = values.length > 1 ? width / (values.length - 1) : width;
        const points = values.map((v, i) => {
          const x = i * stepX;
          const y = height - ((v / max) * (height - 20)) - 10;
          return `${x.toFixed(1)},${y.toFixed(1)}`;
        }).join(' ');
        const gradId = `${svgId}-grad-${di}`;
        html += `<defs><linearGradient id="${gradId}" x1="0" x2="0" y1="0" y2="1">
          <stop offset="0%" stop-color="${fillColor}" stop-opacity="0.7"/>
          <stop offset="100%" stop-color="${fillColor}" stop-opacity="0.05"/>
        </linearGradient></defs>`;
        html += `<polygon class="chart-area" points="${points} ${width},${height} 0,${height}" fill="url(#${gradId})"></polygon>`;
        html += `<polyline class="chart-line" points="${points}" stroke="${color}"></polyline>`;
      });
      svg.innerHTML = html;
    }

    // ---- Heatmap ----
    function renderHeatmap(containerId, coreData) {
      const el = document.getElementById(containerId);
      if (!coreData) { el.innerHTML = ''; return; }
      const entries = Object.entries(coreData).sort((a, b) =>
        parseInt(a[0].replace('cpu', '')) - parseInt(b[0].replace('cpu', ''))
      );
      document.getElementById('core-count').textContent = `${entries.length} cores`;
      el.innerHTML = entries.map(([core, pct]) => {
        const hue = 120 - (pct * 1.2);
        const bg = `hsl(${Math.max(0, hue)}, 75%, ${35 + pct * 0.15}%)`;
        return `<div class="heatmap-cell" style="background:${bg};" title="${core}: ${pct.toFixed(1)}%">${parseInt(core.replace('cpu',''))}</div>`;
      }).join('');
    }

    // ---- Process table ----
    function renderProcessTable(processes) {
      const body = document.getElementById('process-body');
      body.innerHTML = processes.map(p => `
        <tr><td>${p.pid}</td><td>${p.name}</td><td>${Number(p.cpu).toFixed(1)}</td>
        <td>${Number(p.mem).toFixed(1)}</td><td>${p.rss}</td><td>${p.stat || ''}</td></tr>
      `).join('');
    }

    // ---- GPU cards ----
    function renderGpu(gpus) {
      const box = document.getElementById('gpu-box');
      if (!gpus.length) { box.innerHTML = '<div class="gpu-item">No NVIDIA GPU data found.</div>'; return; }
      box.innerHTML = gpus.map(g => {
        const temp = parseFloat(g.temperature);
        const tempColor = temp > 85 ? 'var(--danger)' : temp > 70 ? 'var(--warn)' : 'var(--good)';
        return `<div class="gpu-item">
          <div style="font-weight:700; margin-bottom:4px;">${g.name} #${g.index}</div>
          <div class="tiny">Core: ${g.utilization}% · Mem Util: ${g.memory_utilization}%</div>
          <div class="tiny">VRAM: ${g.memory_used} / ${g.memory_total} MB</div>
          <div class="tiny">Temp: <span style="color:${tempColor};font-weight:600;">${g.temperature}°C</span> · Fan: ${g.fan_speed}%</div>
          <div class="tiny">Power: ${g.power_draw} / ${g.power_limit} W</div>
          <div class="tiny">Clock: ${g.clock_sm} / ${g.clock_sm_max} MHz</div>
        </div>`;
      }).join('');
    }

    // ---- GPU processes ----
    function renderGpuProcesses(procs) {
      const box = document.getElementById('gpu-proc-box');
      if (!procs.length) { box.innerHTML = '<div class="gpu-item" style="font-size:0.82rem;">No active GPU compute processes.</div>'; return; }
      box.innerHTML = procs.map(p =>
        `<div class="gpu-item"><span style="font-weight:600;">PID ${p.pid}</span> · ${p.name} · <span style="color:var(--accent);">${p.gpu_mem_mb} MB</span></div>`
      ).join('');
    }

    // ---- Threshold alerts ----
    function checkAlerts(data) {
      const alertsOn = document.getElementById('alert-toggle').value === 'on';
      const statsCard = document.getElementById('stats-card');
      const panelGpu = document.getElementById('panel-gpu');
      const panelMem = document.getElementById('panel-mem');
      const panelCpu = document.getElementById('panel-cpu');
      const statusEl = document.getElementById('alert-status');
      let alerts = [];

      [statsCard, panelGpu, panelMem, panelCpu].forEach(el => el && el.classList.remove('alert-active'));

      if (!alertsOn) { statusEl.textContent = ''; return; }

      if (data.memory.percent > 90) { alerts.push('⚠️ Memory > 90%'); panelMem && panelMem.classList.add('alert-active'); }
      if (data.cpu_percent > 95) { alerts.push('⚠️ CPU > 95%'); panelCpu && panelCpu.classList.add('alert-active'); }

      const hasGpu = Array.isArray(data.gpu) && data.gpu.length > 0;
      if (hasGpu) {
        data.gpu.forEach(g => {
          const t = parseFloat(g.temperature);
          if (t > 85) { alerts.push(`🔥 GPU#${g.index} Temp ${t}°C`); panelGpu && panelGpu.classList.add('alert-active'); }
        });
      }

      statusEl.textContent = alerts.length ? alerts.join(' | ') : '✅ All clear';
      statusEl.style.color = alerts.length ? 'var(--danger)' : 'var(--good)';
    }

    // ---- Browser notifications ----
    function checkNotifications(data) {
      if (document.getElementById('notif-toggle').value !== 'on') return;
      const hasGpu = Array.isArray(data.gpu) && data.gpu.length > 0;
      if (!hasGpu) return;
      const maxUtil = Math.max(...data.gpu.map(g => parseFloat(g.utilization) || 0));
      const isActive = maxUtil > 5;

      if (lastGpuWasActive === true && !isActive) {
        if (Notification.permission === 'granted') {
          new Notification('🏁 GPU Idle', { body: 'GPU utilization dropped to ~0%. Training may have finished or crashed.' });
        }
      }
      lastGpuWasActive = isActive;
    }

    // Request notification permission on toggle
    document.addEventListener('DOMContentLoaded', () => {
      document.getElementById('notif-toggle').addEventListener('change', function() {
        if (this.value === 'on' && Notification.permission === 'default') {
          Notification.requestPermission();
        }
      });
    });

    // ---- Main refresh ----
    async function refresh() {
      try {
        const response = await fetch('/api/metrics');
        const data = await response.json();

        document.getElementById('host-pill').textContent = `Host: ${data.host} (${data.active_user})`;
        document.getElementById('platform-pill').textContent = `Platform: ${data.platform}`;
        document.getElementById('uptime-pill').textContent = `Uptime: ${formatDuration(data.uptime_seconds)}`;
        document.getElementById('footer').textContent = `Updated at ${new Date(data.timestamp * 1000).toLocaleTimeString()}`;

        if (data.zombies > 0) {
          const zp = document.getElementById('zombie-pill');
          zp.style.display = '';
          zp.textContent = `🧟 Zombies: ${data.zombies}`;
        } else {
          document.getElementById('zombie-pill').style.display = 'none';
        }

        // Bars
        document.getElementById('cpu-value').textContent = `${data.cpu_percent.toFixed(1)}%`;
        document.getElementById('iowait-value').textContent = `${data.iowait_percent.toFixed(1)}%`;
        document.getElementById('mem-value').textContent = `${data.memory.percent.toFixed(1)}%`;
        document.getElementById('disk-value').textContent = `${data.disk.percent.toFixed(1)}%`;
        document.getElementById('net-value').textContent = `${formatBytes(data.network.rx_rate)}/s ↓ · ${formatBytes(data.network.tx_rate)}/s ↑`;

        document.getElementById('cpu-fill').style.width = `${data.cpu_percent}%`;
        document.getElementById('iowait-fill').style.width = `${Math.min(100, data.iowait_percent)}%`;
        document.getElementById('mem-fill').style.width = `${data.memory.percent}%`;
        document.getElementById('disk-fill').style.width = `${data.disk.percent}%`;
        document.getElementById('net-fill').style.width = `${Math.min(100, (data.network.rx_rate + data.network.tx_rate) / (1024 * 1024) * 6)}%`;

        // GPU bars
        let gpuCoreUtil = 0, gpuMemUtil = 0, hasGpu = Array.isArray(data.gpu) && data.gpu.length > 0;
        if (hasGpu) {
          const cu = data.gpu.map(g => Number(g.utilization)).filter(Number.isFinite);
          const mu = data.gpu.map(g => Number(g.memory_utilization)).filter(Number.isFinite);
          if (cu.length) gpuCoreUtil = cu.reduce((a, b) => a + b, 0) / cu.length;
          if (mu.length) gpuMemUtil = mu.reduce((a, b) => a + b, 0) / mu.length;
        }
        document.getElementById('gpu-core-value').textContent = hasGpu ? `${gpuCoreUtil.toFixed(1)}%` : 'N/A';
        document.getElementById('gpu-mem-value').textContent = hasGpu ? `${gpuMemUtil.toFixed(1)}%` : 'N/A';
        document.getElementById('gpu-core-fill').style.width = hasGpu ? `${gpuCoreUtil}%` : '0%';
        document.getElementById('gpu-mem-fill').style.width = hasGpu ? `${gpuMemUtil}%` : '0%';

        // Histories
        pushHistory(cpuHistory, data.cpu_percent);
        pushHistory(iowaitHistory, data.iowait_percent);
        pushHistory(memHistory, data.memory.percent);
        pushHistory(netHistory, Math.min(100, ((data.network.rx_rate + data.network.tx_rate) / (1024 * 1024)) * 10));
        pushHistory(gpuUtilHistory, gpuCoreUtil);
        pushHistory(gpuMemHistory, gpuMemUtil);
        const gth = data.gpu_history;
        pushHistory(gpuTempHistory, gth && gth.temp && gth.temp.length ? gth.temp[gth.temp.length-1] : 0);
        pushHistory(gpuPowerHistory, gth && gth.power && gth.power.length ? gth.power[gth.power.length-1] : 0);
        pushHistory(diskReadHistory, data.disk_io.read_rate);
        pushHistory(diskWriteHistory, data.disk_io.write_rate);

        // Charts
        renderChart('cpu-chart', [
          { values: cpuHistory, color: '#60a5fa', fillColor: '#60a5fa' },
          { values: iowaitHistory, color: '#f97316', fillColor: '#f97316' },
        ]);
        renderChart('mem-chart', [{ values: memHistory, color: '#fbbf24', fillColor: '#fbbf24' }]);
        renderChart('net-chart', [
          { values: data.network.rx_history.map(v => Math.min(v / 1024, 1000)), color: '#4ade80', fillColor: '#4ade80' },
          { values: data.network.tx_history.map(v => Math.min(v / 1024, 1000)), color: '#60a5fa', fillColor: '#60a5fa' },
        ]);
        renderChart('gpu-util-chart', [{ values: gpuUtilHistory, color: '#22d3ee', fillColor: '#22d3ee' }]);
        renderChart('gpu-extra-chart', [
          { values: gpuMemHistory, color: '#4ade80', fillColor: '#4ade80' },
          { values: gpuTempHistory, color: '#f97316', fillColor: '#f97316' },
          { values: gpuPowerHistory, color: '#60a5fa', fillColor: '#60a5fa' },
        ]);
        renderChart('diskio-chart', [
          { values: diskReadHistory.map(v => v / 1024), color: '#4ade80', fillColor: '#4ade80' },
          { values: diskWriteHistory.map(v => v / 1024), color: '#60a5fa', fillColor: '#60a5fa' },
        ]);
        document.getElementById('diskio-rates').textContent =
          `R: ${formatBytes(data.disk_io.read_rate)}/s · W: ${formatBytes(data.disk_io.write_rate)}/s`;

        // Heatmap
        renderHeatmap('cpu-heatmap', data.cpu_cores);

        // Memory details
        const m = data.memory;
        document.getElementById('mem-details').innerHTML = `
          <div>Used: ${formatBytes(m.used)} / ${formatBytes(m.total)}</div>
          <div>Swap: ${formatBytes(m.swap_used)} / ${formatBytes(m.swap_total)} (${m.swap_percent.toFixed(1)}%)</div>
          <div>Cached: ${formatBytes(m.cached)}</div>
          <div>Buffers: ${formatBytes(m.buffers)}</div>
          <div>Slab: ${formatBytes(m.slab)}</div>
        `;

        // System info box
        const temps = (data.cpu_temps || []).map(t => `${t.type}: <span style="font-weight:600;color:${t.temp_c > 80 ? 'var(--danger)' : t.temp_c > 60 ? 'var(--warn)' : 'var(--good)'};">${t.temp_c}°C</span>`).join('<br>');
        document.getElementById('sysinfo-box').innerHTML = `
          <div class="gpu-item"><div class="label">Load Average</div>
            1m: ${data.load_average['1m'].toFixed(2)} · 5m: ${data.load_average['5m'].toFixed(2)} · 15m: ${data.load_average['15m'].toFixed(2)}</div>
          <div class="gpu-item"><div class="label">Disk</div>
            ${data.disk.path}<br>Used: ${formatBytes(data.disk.used)} / ${formatBytes(data.disk.total)}<br>Free: ${formatBytes(data.disk.free)}</div>
          <div class="gpu-item"><div class="label">Temperatures</div>${temps || 'No thermal data'}</div>
          <div class="gpu-item"><div class="label">Scheduler</div>
            Running: ${data.procs_running} · Blocked: ${data.procs_blocked}<br>
            Ctx switches: ${Math.round(data.ctxt_rate)}/s</div>
        `;

        renderProcessTable(data.processes);
        renderGpu(data.gpu);
        renderGpuProcesses(data.gpu_processes || []);
        checkAlerts(data);
        checkNotifications(data);
      } catch (err) {
        console.error('Refresh error:', err);
      }
    }

    refresh();
    refreshTimer = setInterval(refresh, refreshInterval);
  </script>
</body>
</html>
"""


class DashboardHandler(BaseHTTPRequestHandler):
    server_version = "ResourceDashboard/2.0"

    def _send_json(self, payload: Dict[str, object]) -> None:
        data = json.dumps(payload).encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(data)

    def _send_html(self, html: str) -> None:
        data = html.encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(data)

    def _send_csv(self, csv_text: str) -> None:
        data = csv_text.encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "text/csv; charset=utf-8")
        self.send_header("Content-Disposition", "attachment; filename=resource_metrics.csv")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self) -> None:  # noqa: N802 - required by BaseHTTPRequestHandler
        parsed = urlparse(self.path)
        if parsed.path == "/":
            self._send_html(HTML_TEMPLATE)
            return
        if parsed.path == "/api/metrics":
            root = Path(self.server.sample_root)  # type: ignore[attr-defined]
            top_limit = int(self.server.top_limit)  # type: ignore[attr-defined]
            self._send_json(sample_metrics(root, top_limit))
            return
        if parsed.path == "/api/export_csv":
            self._send_csv(generate_csv())
            return
        self.send_error(404, "Not Found")

    def log_message(self, format: str, *args: object) -> None:  # noqa: A003 - signature required
        return


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Interactive local resource usage dashboard.")
    parser.add_argument("--host", default="127.0.0.1", help="Bind address for the local dashboard.")
    parser.add_argument("--port", type=int, default=DEFAULT_PORT, help="Port to serve the dashboard on.")
    parser.add_argument("--root", default=str(Path.home()), help="Filesystem root to summarize for disk usage.")
    parser.add_argument("--top", type=int, default=DEFAULT_TOP_PROCESSES, help="Number of top processes to display.")
    parser.add_argument("--no-browser", action="store_true", help="Do not open the dashboard automatically in a browser.")
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    sample_root = Path(args.root).expanduser().resolve()
    if not sample_root.exists():
        raise SystemExit(f"Path does not exist: {sample_root}")

    server = ThreadingHTTPServer((args.host, args.port), DashboardHandler)
    server.sample_root = sample_root  # type: ignore[attr-defined]
    server.top_limit = args.top  # type: ignore[attr-defined]

    url = f"http://{args.host}:{args.port}/"
    print(f"Serving resource dashboard at {url}")
    print(f"Monitoring disk path: {sample_root}")

    if not args.no_browser:
      webbrowser.open(url)

    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nShutting down resource dashboard...")
    finally:
        server.server_close()


if __name__ == "__main__":
    main()