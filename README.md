# Resource Dashboard

![Introduction](Images/Introduction.png)

A native Linux desktop application and standalone HTTP server for live system resource monitoring. Built to monitor CPU, memory, GPU, disk I/O, and network activity during heavy workloads (like image generation and model training).

> [!NOTE]
> Currently, this application is only supported on Linux. Support for Windows and macOS is coming soon!

---

## 📸 Dashboard Gallery

### 1. System Overview
![System Overview](Images/System%20OVerview.png)

### 2. CPU & Memory Monitor
![CPU & Memory Monitor](Images/CPU,memory%20monitor.png)

### 3. GPU Telemetry & Threshold Warnings
![GPU Check and warning](Images/GPU%20Check%20and%20warning%20.png)

---

## ✨ Features

- **GPU Telemetry:** Tracks GPU core/memory utilization, temperatures, power draw, and fan speed (via `nvidia-smi`).
- **CPU & Memory:** Per-core CPU heatmap, IO-wait tracking, and detailed memory stats (swap, cached, buffers).
- **Disk & Network:** Disk I/O read/write rates and network rx/tx throughput.
- **Alerts & Notifications:** Threshold alerts for high temperature or utilization, plus desktop notifications when the GPU drops to idle (useful to know when training finishes).
- **Export & History:** Download historical metrics as a CSV file.
- **Native Desktop App:** GTK3 + WebKit2 wrapper offering a system tray icon, keyboard shortcuts, always-on-top pinning, and fullscreen mode.

## 🛠 Prerequisites

To get full functionality (especially the GPU telemetry), your system should have the NVIDIA driver utilities installed:
- `nvidia-smi` must be accessible via your terminal. *(If this is missing, the dashboard will still function perfectly but will omit the GPU section).*

## 🚀 Installation (Linux)

### Method 1: Using the `.deb` Package (Recommended for Debian/Ubuntu)

1. **[Download the latest `.deb` package](https://github.com/Abhishek-Durgude/Resource-Monitor/raw/main/resource-dashboard_1.0-1_all.deb)** from this repository.
2. Install it using `apt` (this automatically handles required dependencies):
   ```bash
   sudo apt install ./resource-dashboard_1.0-1_all.deb
   ```
3. You can now launch it from your application menu or terminal!

### Method 2: Manual Installation Script

If you aren't on a Debian-based system or prefer a manual script:

1. Clone or download this repository.
2. Run the installer script:
   ```bash
   bash install_dashboard.sh
   ```
3. The installer will check for missing dependencies (`gir1.2-webkit2-4.0`, etc.) and install them, add a desktop entry so you can find it in your app launcher, and create a terminal shortcut.

## 💻 Usage

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

## ⌨️ Keyboard Shortcuts

When the dashboard is open, you can use these shortcuts:
- `Ctrl + R`: Reload the dashboard
- `Ctrl + =`: Zoom In
- `Ctrl + -`: Zoom Out
- `Ctrl + 0`: Reset Zoom
- `F11`: Toggle Fullscreen
- `Ctrl + Q`: Quit Application

## 🗑 Uninstallation

If you installed via the `.deb` package, you can cleanly remove the dashboard with:
```bash
sudo apt remove resource-dashboard
```
