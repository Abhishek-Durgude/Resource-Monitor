#!/usr/bin/env bash
# ─────────────────────────────────────────────────────────────────────────────
#  Resource Dashboard – Linux Desktop App Installer
#  AIIMS Rishikesh · Burn Image Generation Project
# ─────────────────────────────────────────────────────────────────────────────
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
DESKTOP_DIR="$HOME/.local/share/applications"
ICON_DIR="$HOME/.local/share/icons"
APP_NAME="resource-dashboard"

# Colors
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
CYAN='\033[0;36m'
RED='\033[0;31m'
BOLD='\033[1m'
NC='\033[0m'

echo ""
echo -e "${CYAN}${BOLD}╔════════════════════════════════════════════════════╗${NC}"
echo -e "${CYAN}${BOLD}║   🖥️  Resource Dashboard — Linux App Installer    ║${NC}"
echo -e "${CYAN}${BOLD}║         AIIMS Rishikesh Project                   ║${NC}"
echo -e "${CYAN}${BOLD}╚════════════════════════════════════════════════════╝${NC}"
echo ""

# ── Step 1: Check Python ─────────────────────────────────────────────────────
echo -e "${BOLD}[1/5] Checking Python 3...${NC}"
if command -v python3 &>/dev/null; then
    PY_VERSION=$(python3 --version 2>&1)
    echo -e "  ${GREEN}✓${NC} $PY_VERSION"
else
    echo -e "  ${RED}✗ Python 3 not found. Please install python3.${NC}"
    exit 1
fi

# ── Step 2: Check GTK/WebKit ─────────────────────────────────────────────────
echo -e "${BOLD}[2/5] Checking GTK3 + WebKit2 libraries...${NC}"
MISSING_DEPS=""

if python3 -c "import gi; gi.require_version('Gtk', '3.0'); from gi.repository import Gtk" 2>/dev/null; then
    echo -e "  ${GREEN}✓${NC} GTK 3.0"
else
    echo -e "  ${RED}✗${NC} GTK 3.0 — not found"
    MISSING_DEPS="python3-gi gir1.2-gtk-3.0"
fi

WEBKIT_OK=false
if python3 -c "import gi; gi.require_version('WebKit2', '4.1'); from gi.repository import WebKit2" 2>/dev/null; then
    echo -e "  ${GREEN}✓${NC} WebKit2 4.1"
    WEBKIT_OK=true
elif python3 -c "import gi; gi.require_version('WebKit2', '4.0'); from gi.repository import WebKit2" 2>/dev/null; then
    echo -e "  ${GREEN}✓${NC} WebKit2 4.0"
    WEBKIT_OK=true
else
    echo -e "  ${RED}✗${NC} WebKit2 — not found"
    MISSING_DEPS="$MISSING_DEPS gir1.2-webkit2-4.0"
fi

if [[ -n "$MISSING_DEPS" ]]; then
    echo ""
    echo -e "${YELLOW}Missing dependencies detected. Installing...${NC}"
    echo -e "  Running: ${BOLD}sudo apt install -y $MISSING_DEPS${NC}"
    echo ""
    sudo apt install -y $MISSING_DEPS
    echo ""
    echo -e "  ${GREEN}✓${NC} Dependencies installed"
fi

# ── Step 3: Install Icon ─────────────────────────────────────────────────────
echo -e "${BOLD}[3/5] Installing application icon...${NC}"
mkdir -p "$ICON_DIR"

ICON_SRC="$SCRIPT_DIR/resource_dashboard_icon.svg"
ICON_DST="$ICON_DIR/$APP_NAME.svg"

if [[ -f "$ICON_SRC" ]]; then
    cp "$ICON_SRC" "$ICON_DST"
    echo -e "  ${GREEN}✓${NC} Icon installed to $ICON_DST"
else
    echo -e "  ${YELLOW}⚠${NC} Icon file not found at $ICON_SRC, using system icon"
    ICON_DST="utilities-system-monitor"
fi

# ── Step 4: Install .desktop Entry ───────────────────────────────────────────
echo -e "${BOLD}[4/5] Creating application launcher...${NC}"
mkdir -p "$DESKTOP_DIR"

DESKTOP_FILE="$DESKTOP_DIR/$APP_NAME.desktop"
cat > "$DESKTOP_FILE" << EOF
[Desktop Entry]
Version=1.0
Type=Application
Name=Resource Dashboard
GenericName=System Monitor
Comment=Live resource monitoring dashboard for CPU, GPU, Memory, Disk, and Network — AIIMS Rishikesh
Exec=python3 "$SCRIPT_DIR/resource_dashboard_app.py"
Icon=$ICON_DST
Terminal=false
Categories=System;Monitor;Utility;
Keywords=monitor;cpu;gpu;memory;dashboard;resources;nvidia;system;
StartupNotify=true
StartupWMClass=resource-dashboard
EOF

chmod +x "$DESKTOP_FILE"
echo -e "  ${GREEN}✓${NC} Launcher created at $DESKTOP_FILE"

# Update desktop database
if command -v update-desktop-database &>/dev/null; then
    update-desktop-database "$DESKTOP_DIR" 2>/dev/null || true
fi

# ── Step 5: Create CLI shortcut ──────────────────────────────────────────────
echo -e "${BOLD}[5/5] Creating command-line shortcut...${NC}"

LOCAL_BIN="$HOME/.local/bin"
mkdir -p "$LOCAL_BIN"

SYMLINK="$LOCAL_BIN/resource-dashboard"
cat > "$SYMLINK" << EOF
#!/usr/bin/env bash
# Resource Dashboard — CLI Launcher
exec python3 "$SCRIPT_DIR/resource_dashboard_app.py" "\$@"
EOF
chmod +x "$SYMLINK"
echo -e "  ${GREEN}✓${NC} CLI command: ${BOLD}resource-dashboard${NC}"

# Check if ~/.local/bin is in PATH
if [[ ":$PATH:" != *":$LOCAL_BIN:"* ]]; then
    echo -e "  ${YELLOW}⚠${NC} $LOCAL_BIN is not in your PATH."
    echo -e "    Add this to your ~/.bashrc or ~/.zshrc:"
    echo -e "    ${CYAN}export PATH=\"\$HOME/.local/bin:\$PATH\"${NC}"
fi

# ── Done ─────────────────────────────────────────────────────────────────────
echo ""
echo -e "${GREEN}${BOLD}╔════════════════════════════════════════════════════╗${NC}"
echo -e "${GREEN}${BOLD}║   ✅  Installation Complete!                      ║${NC}"
echo -e "${GREEN}${BOLD}╚════════════════════════════════════════════════════╝${NC}"
echo ""
echo -e "  ${BOLD}Launch methods:${NC}"
echo -e "    1. Search ${CYAN}\"Resource Dashboard\"${NC} in your app launcher"
echo -e "    2. Run ${CYAN}resource-dashboard${NC} from terminal"
echo -e "    3. Run ${CYAN}python3 $SCRIPT_DIR/resource_dashboard_app.py${NC}"
echo ""
echo -e "  ${BOLD}Keyboard shortcuts (inside the app):${NC}"
echo -e "    Ctrl+R   Reload    |  Ctrl+=  Zoom In   |  F11  Fullscreen"
echo -e "    Ctrl+Q   Quit      |  Ctrl+-  Zoom Out  |  Ctrl+0  Reset Zoom"
echo ""
echo -e "  ${BOLD}CLI options:${NC}"
echo -e "    ${CYAN}resource-dashboard --root /path/to/monitor${NC}"
echo -e "    ${CYAN}resource-dashboard --top 12 --zoom 0.9${NC}"
echo ""

# Ask to launch
read -p "  Launch the dashboard now? [Y/n] " -n 1 -r
echo ""
if [[ ! $REPLY =~ ^[Nn]$ ]]; then
    echo -e "  ${CYAN}Starting Resource Dashboard...${NC}"
    nohup python3 "$SCRIPT_DIR/resource_dashboard_app.py" &>/dev/null &
    disown
    echo -e "  ${GREEN}✓${NC} Dashboard launched!"
fi

echo ""
