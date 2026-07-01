# Resource Dashboard

A native Linux desktop application and standalone HTTP server for live system resource monitoring. Built for the AIIMS Rishikesh project to monitor CPU, memory, GPU, disk I/O, and network activity during heavy workloads (like image generation and model training).

## Features
- **GPU Telemetry:** Tracks GPU core/memory utilization, temperatures, power draw, and fan speed (via `nvidia-smi`).
- **CPU & Memory:** Per-core CPU heatmap, IO-wait tracking, and detailed memory stats (swap, cached, buffers).
- **Disk & Network:** Disk I/O read/write rates and network rx/tx throughput.
- **Alerts & Notifications:** Threshold alerts for high temperature or utilization, plus desktop notifications when the GPU drops to idle (useful to know when training finishes).
- **Export & History:** Download historical metrics as a CSV file.
- **Native Desktop App:** GTK3 + WebKit2 wrapper offering a system tray icon, keyboard shortcuts, always-on-top pinning, and fullscreen mode.

## Installation (Linux)

You can install the dashboard as a native desktop application using the provided install script.

1. Clone or download this repository.
2. Run the installer script:
   ```bash
   bash install_dashboard.sh
   ```
3. The installer will check for missing dependencies (`gir1.2-webkit2-4.0`, etc.) and install them, add a desktop entry so you can find it in your app launcher, and create a terminal shortcut.

## Usage

**From your app launcher:**
Search for "Resource Dashboard" and open it.

**From the terminal:**
```bash
resource-dashboard
```

**Options:**
```bash
resource-dashboard --root /data/datasets   # Monitor a specific disk path
resource-dashboard --top 12                # Show top 12 processes instead of 8
resource-dashboard --zoom 0.85             # Set initial UI zoom to 85%
```
