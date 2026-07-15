#!/usr/bin/env python3
"""Interactive local dashboard for monitoring computer resource usage.

This starts a small HTTP server on localhost and serves a live dashboard
showing CPU, memory, disk, network, and process activity. It only uses the
Python standard library, so it can run without extra packages.
"""

from __future__ import annotations

import argparse
import base64
import logging
import configparser
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
from typing import Any, Deque, Dict, List, Optional, Tuple, Union
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


def sanitize_for_json(obj: Any) -> Any:
    """Replace NaN / inf / -inf floats with None so json.dumps never fails."""
    if isinstance(obj, float):
        if obj != obj or obj == float('inf') or obj == float('-inf'):
            return None
        return obj
    if isinstance(obj, dict):
        return {k: sanitize_for_json(v) for k, v in obj.items()}
    if isinstance(obj, (list, tuple)):
        return [sanitize_for_json(v) for v in obj]
    return obj


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
# GPU  (with caching – avoids duplicate nvidia-smi calls per sample cycle)
# ---------------------------------------------------------------------------
_gpu_cache: Dict[str, Any] = {"info": [], "procs": [], "timestamp": 0.0}
_GPU_CACHE_TTL = 0.5  # seconds


def _refresh_gpu_cache() -> None:
    """Populate the module-level GPU cache if it is stale."""
    now = time.time()
    if now - _gpu_cache["timestamp"] < _GPU_CACHE_TTL:
        return

    # --- GPU info ---
    info_cmd = [
        "nvidia-smi",
        "--query-gpu=index,name,utilization.gpu,utilization.memory,memory.used,memory.total,temperature.gpu,power.draw,power.limit,fan.speed,clocks.current.sm,clocks.max.sm",
        "--format=csv,noheader,nounits",
    ]
    gpus: List[Dict[str, str]] = []
    try:
        output = subprocess.check_output(info_cmd, stderr=subprocess.DEVNULL, text=True)
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
    except Exception:
        pass

    # --- GPU processes ---
    proc_cmd = [
        "nvidia-smi",
        "--query-compute-apps=pid,process_name,used_gpu_memory,gpu_bus_id",
        "--format=csv,noheader,nounits",
    ]
    processes: List[Dict[str, str]] = []
    try:
        output = subprocess.check_output(proc_cmd, stderr=subprocess.DEVNULL, text=True)
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
    except Exception:
        pass

    _gpu_cache["info"] = gpus
    _gpu_cache["procs"] = processes
    _gpu_cache["timestamp"] = now


def read_gpu_info() -> List[Dict[str, str]]:
    _refresh_gpu_cache()
    return _gpu_cache["info"]


def read_gpu_processes() -> List[Dict[str, str]]:
    """Return list of processes running on GPUs via nvidia-smi."""
    _refresh_gpu_cache()
    return _gpu_cache["procs"]


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
import sys

def get_html_template():
    template_path = Path(__file__).parent / 'dashboard.html'
    if template_path.exists():
        return template_path.read_text(encoding='utf-8')
    return "<html><body>Error: dashboard.html not found</body></html>"

HTML_TEMPLATE = get_html_template()


class DashboardHandler(BaseHTTPRequestHandler):
    server_version = "ResourceDashboard/2.0"

    def _add_security_headers(self, is_api: bool = False) -> None:
        """Add common security headers to every response."""
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("X-Frame-Options", "DENY")
        if is_api:
            host = self.headers.get("Host", f"{self.server.server_address[0]}:{self.server.server_address[1]}")
            self.send_header("Access-Control-Allow-Origin", f"http://{host}")

    def _send_json(self, payload: Dict[str, object]) -> None:
        data = json.dumps(sanitize_for_json(payload)).encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        self._add_security_headers(is_api=True)
        self.end_headers()
        self.wfile.write(data)

    def _send_html(self, html: str) -> None:
        data = html.encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        self._add_security_headers()
        self.end_headers()
        self.wfile.write(data)

    def _send_csv(self, csv_text: str) -> None:
        data = csv_text.encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "text/csv; charset=utf-8")
        self.send_header("Content-Disposition", "attachment; filename=resource_metrics.csv")
        self.send_header("Content-Length", str(len(data)))
        self._add_security_headers(is_api=True)
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self) -> None:  # noqa: N802 - required by BaseHTTPRequestHandler
        # Auth check
        expected_auth = getattr(self.server, 'auth', None)
        if expected_auth:
            auth_header = self.headers.get('Authorization')
            if not auth_header or not auth_header.startswith('Basic '):
                self.send_response(401)
                self.send_header('WWW-Authenticate', 'Basic realm="Dashboard"')
                self.end_headers()
                self.wfile.write(b"Authentication required")
                return
            
            encoded = auth_header.split(' ', 1)[1]
            try:
                decoded = base64.b64decode(encoded).decode('utf-8')
                if decoded != expected_auth:
                    raise ValueError("Invalid auth")
            except Exception:
                self.send_response(401)
                self.send_header('WWW-Authenticate', 'Basic realm="Dashboard"')
                self.end_headers()
                self.wfile.write(b"Authentication failed")
                return

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
    parser.add_argument("--auth", help="Basic auth credentials in format user:pass")
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    
    # Setup logging
    logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')
    
    # Load config
    config = configparser.ConfigParser()
    config_path = Path.home() / '.config' / 'resource-dashboard' / 'config.ini'
    if config_path.exists():
        config.read(config_path)
        if 'Server' in config:
            args.host = config['Server'].get('host', args.host)
            args.port = config['Server'].getint('port', args.port)
            args.auth = config['Server'].get('auth', getattr(args, 'auth', None))
    sample_root = Path(args.root).expanduser().resolve()
    if not sample_root.exists():
        raise SystemExit(f"Path does not exist: {sample_root}")

    server = ThreadingHTTPServer((args.host, args.port), DashboardHandler)
    server.sample_root = sample_root  # type: ignore[attr-defined]
    server.top_limit = args.top  # type: ignore[attr-defined]
    server.auth = args.auth  # type: ignore[attr-defined]

    url = f"http://{args.host}:{args.port}/"
    logging.info(f"Serving resource dashboard at {url}")
    logging.info(f"Monitoring disk path: {sample_root}")

    if not args.no_browser:
      webbrowser.open(url)

    try:
        server.serve_forever()
    except KeyboardInterrupt:
        logging.info("Shutting down resource dashboard...")
    finally:
        server.server_close()


if __name__ == "__main__":
    main()