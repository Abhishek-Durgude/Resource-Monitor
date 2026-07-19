#!/usr/bin/env python3
"""
Resource Dashboard — Native Linux Desktop Application

A standalone GTK3 + WebKit2 desktop application that provides real-time
system resource monitoring. Wraps the resource_dashboard HTTP server in
a native window with system tray integration, keyboard shortcuts, and
proper desktop application behavior.

Usage:
    python3 resource_dashboard_app.py [--root /path] [--top 8]

Dependencies (Ubuntu/Debian):
    sudo apt install python3-gi gir1.2-webkit2-4.0 gir1.2-gtk-3.0
"""

from __future__ import annotations

import argparse
import json
import os
import signal
import sys
import socket
import threading
import time
from pathlib import Path

# ── GTK / WebKit Imports ─────────────────────────────────────────────────────
try:
    import gi

    gi.require_version("Gtk", "3.0")
    from gi.repository import Gtk, Gdk, GLib, Gio  # noqa: E402

    # Try WebKit2 4.1 first (newer distros), fall back to 4.0
    try:
        gi.require_version("WebKit2", "4.1")
    except ValueError:
        gi.require_version("WebKit2", "4.0")
    from gi.repository import WebKit2  # noqa: E402

except (ImportError, ValueError) as exc:
    print(f"Error: Required GTK/WebKit libraries not found: {exc}")
    print()
    print("Install them with:")
    print("  Ubuntu/Debian : sudo apt install python3-gi gir1.2-webkit2-4.0 gir1.2-gtk-3.0")
    print("  Fedora        : sudo dnf install python3-gobject webkit2gtk3")
    print("  Arch          : sudo pacman -S python-gobject webkit2gtk")
    sys.exit(1)

# ── Optional: AppIndicator for system tray ───────────────────────────────────
HAS_APPINDICATOR = False
try:
    gi.require_version("AppIndicator3", "0.1")
    from gi.repository import AppIndicator3  # noqa: E402

    HAS_APPINDICATOR = True
except (ValueError, ImportError):
    pass

# ── Import the dashboard server from the sibling module ──────────────────────
SCRIPT_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(SCRIPT_DIR))

from resource_dashboard import (  # noqa: E402
    DashboardHandler,
    sample_metrics,
    HTML_TEMPLATE,
    DEFAULT_TOP_PROCESSES,
)
from http.server import ThreadingHTTPServer  # noqa: E402


# ─────────────────────────────────────────────────────────────────────────────
# Utility: find a free port
# ─────────────────────────────────────────────────────────────────────────────
def find_free_port(start: int = 18700, end: int = 18800) -> int:
    """Find an available TCP port in the given range."""
    for port in range(start, end):
        try:
            with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
                sock.bind(("127.0.0.1", port))
                return port
        except OSError:
            continue
    raise RuntimeError(f"No free port found in range {start}-{end}")


# ─────────────────────────────────────────────────────────────────────────────
# Utility: wait for the HTTP server to be ready
# ─────────────────────────────────────────────────────────────────────────────
def wait_for_server(host: str, port: int, timeout: float = 10.0) -> None:
    """Block until the server accepts TCP connections, or *timeout* seconds elapse.

    Retries every 0.2 s.  If the timeout is reached a warning is printed but
    execution continues so the GTK window can still attempt to load.
    """
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
                s.settimeout(1.0)
                s.connect((host if host != "0.0.0.0" else "127.0.0.1", port))
                return  # server is ready
        except OSError:
            time.sleep(0.2)
    print(
        f"Warning: server on {host}:{port} did not respond within {timeout}s; "
        "attempting to load anyway.",
        file=sys.stderr,
    )


# ─────────────────────────────────────────────────────────────────────────────
# Window State Persistence
# ─────────────────────────────────────────────────────────────────────────────
class WindowStatePersistence:
    """Save and restore window size and position via a JSON config file.

    Saves are debounced (default 500 ms) so rapid configure events don't
    cause excessive disk I/O.
    """

    CONFIG_DIR = Path.home() / ".config" / "resource-dashboard"
    CONFIG_FILE = CONFIG_DIR / "window_state.json"

    def __init__(self, window: Gtk.Window, debounce_ms: int = 500):
        self._window = window
        self._debounce_ms = debounce_ms
        self._save_timer_id: int | None = None
        self._restore()
        window.connect("configure-event", self._on_configure)

    # ── Restore ───────────────────────────────────────────────────────────
    def _restore(self) -> None:
        """Load previously-saved geometry and apply it (with screen-bounds check)."""
        if not self.CONFIG_FILE.is_file():
            return
        try:
            state = json.loads(self.CONFIG_FILE.read_text())
        except (json.JSONDecodeError, OSError):
            return

        width = state.get("width", 1400)
        height = state.get("height", 900)
        x = state.get("x")
        y = state.get("y")

        # Screen-bounds sanity check
        screen = self._window.get_screen()
        if screen:
            sw = screen.get_width()
            sh = screen.get_height()
            width = min(width, sw)
            height = min(height, sh)
            if x is not None and y is not None:
                x = max(0, min(x, sw - 100))
                y = max(0, min(y, sh - 100))

        self._window.set_default_size(width, height)
        if x is not None and y is not None:
            self._window.move(x, y)

    # ── Save (debounced) ──────────────────────────────────────────────────
    def _on_configure(self, widget, event) -> bool:
        """Schedule a debounced save whenever the window is moved or resized."""
        if self._save_timer_id is not None:
            GLib.source_remove(self._save_timer_id)
        self._save_timer_id = GLib.timeout_add(self._debounce_ms, self._do_save)
        return False  # propagate event

    def _do_save(self) -> bool:
        """Actually persist the current geometry to disk."""
        self._save_timer_id = None
        win = self._window.get_window()
        if win is None:
            return False  # GLib.SOURCE_REMOVE
        size = self._window.get_size()
        pos = self._window.get_position()
        state = {
            "width": size.width,
            "height": size.height,
            "x": pos.root_x,
            "y": pos.root_y,
        }
        try:
            self.CONFIG_DIR.mkdir(parents=True, exist_ok=True)
            self.CONFIG_FILE.write_text(json.dumps(state, indent=2))
        except OSError as exc:
            print(f"Warning: could not save window state: {exc}", file=sys.stderr)
        return False  # GLib.SOURCE_REMOVE

    def save_now(self) -> None:
        """Immediately persist the current window geometry (call before hide/quit)."""
        if self._save_timer_id is not None:
            GLib.source_remove(self._save_timer_id)
            self._save_timer_id = None
        self._do_save()


# ─────────────────────────────────────────────────────────────────────────────
# Dashboard Server Thread
# ─────────────────────────────────────────────────────────────────────────────
class DashboardServerThread(threading.Thread):
    """Runs the resource dashboard HTTP server in a background daemon thread."""

    def __init__(self, host: str, port: int, sample_root: Path, top_limit: int):
        super().__init__(daemon=True, name="DashboardServer")
        self.host = host
        self.port = port
        self.sample_root = sample_root
        self.top_limit = top_limit
        self.server: ThreadingHTTPServer | None = None

    def run(self) -> None:
        self.server = ThreadingHTTPServer((self.host, self.port), DashboardHandler)
        self.server.sample_root = self.sample_root  # type: ignore[attr-defined]
        self.server.top_limit = self.top_limit  # type: ignore[attr-defined]
        self.server.serve_forever()

    def stop(self) -> None:
        if self.server:
            self.server.shutdown()


# ─────────────────────────────────────────────────────────────────────────────
# Main Application Window
# ─────────────────────────────────────────────────────────────────────────────
class DashboardWindow(Gtk.Window):
    """Native GTK window hosting the resource dashboard via WebKit2."""

    def __init__(self, url: str, icon_path: str | None = None):
        super().__init__(title="Resource Dashboard")
        self.url = url
        self._setup_window(icon_path)
        self._setup_header_bar()
        self._setup_webview()
        self._setup_shortcuts()

    def _setup_window(self, icon_path: str | None) -> None:
        self.set_default_size(1400, 900)
        self.set_position(Gtk.WindowPosition.CENTER)
        self.connect("destroy", self._on_destroy)
        self.connect("delete-event", self._on_delete_event)

        # Dark theme
        settings = Gtk.Settings.get_default()
        if settings:
            settings.set_property("gtk-application-prefer-dark-theme", True)

        # Window icon
        if icon_path and os.path.isfile(icon_path):
            try:
                self.set_icon_from_file(icon_path)
            except GLib.Error:
                pass

        # Transparent background
        screen = self.get_screen()
        visual = screen.get_rgba_visual()
        if visual:
            self.set_visual(visual)

    def _setup_header_bar(self) -> None:
        header = Gtk.HeaderBar()
        header.set_show_close_button(True)
        header.set_title("🖥️ Resource Dashboard")
        header.set_subtitle("Live System Monitor · by Clixzsera Lab, Abhishek Durgude")

        # Reload button
        reload_btn = Gtk.Button()
        reload_icon = Gio.ThemedIcon(name="view-refresh-symbolic")
        reload_img = Gtk.Image.new_from_gicon(reload_icon, Gtk.IconSize.BUTTON)
        reload_btn.add(reload_img)
        reload_btn.set_tooltip_text("Reload (Ctrl+R)")
        reload_btn.connect("clicked", self._on_reload)
        header.pack_start(reload_btn)

        # Zoom controls
        zoom_out_btn = Gtk.Button(label="−")
        zoom_out_btn.set_tooltip_text("Zoom Out (Ctrl+-)")
        zoom_out_btn.connect("clicked", self._on_zoom_out)
        header.pack_start(zoom_out_btn)

        zoom_reset_btn = Gtk.Button(label="100%")
        zoom_reset_btn.set_tooltip_text("Reset Zoom (Ctrl+0)")
        zoom_reset_btn.connect("clicked", self._on_zoom_reset)
        self._zoom_label = zoom_reset_btn
        header.pack_start(zoom_reset_btn)

        zoom_in_btn = Gtk.Button(label="+")
        zoom_in_btn.set_tooltip_text("Zoom In (Ctrl+=)")
        zoom_in_btn.connect("clicked", self._on_zoom_in)
        header.pack_start(zoom_in_btn)

        # Always-on-top toggle
        pin_btn = Gtk.ToggleButton()
        pin_icon = Gio.ThemedIcon(name="view-pin-symbolic")
        pin_img = Gtk.Image.new_from_gicon(pin_icon, Gtk.IconSize.BUTTON)
        pin_btn.add(pin_img)
        pin_btn.set_tooltip_text("Always on Top")
        pin_btn.connect("toggled", self._on_pin_toggled)
        header.pack_end(pin_btn)

        # Fullscreen toggle
        fs_btn = Gtk.Button()
        fs_icon = Gio.ThemedIcon(name="view-fullscreen-symbolic")
        fs_img = Gtk.Image.new_from_gicon(fs_icon, Gtk.IconSize.BUTTON)
        fs_btn.add(fs_img)
        fs_btn.set_tooltip_text("Fullscreen (F11)")
        fs_btn.connect("clicked", self._on_fullscreen)
        header.pack_end(fs_btn)

        self.set_titlebar(header)

    def _setup_webview(self) -> None:
        # WebKit settings
        wk_settings = WebKit2.Settings()
        wk_settings.set_property("enable-javascript", True)
        wk_settings.set_property("enable-developer-extras", True)
        wk_settings.set_property("enable-smooth-scrolling", True)

        # Try to set hardware acceleration (may not be available on all versions)
        try:
            wk_settings.set_property("hardware-acceleration-policy",
                                     WebKit2.HardwareAccelerationPolicy.ALWAYS)
        except Exception:
            pass

        # Create WebView
        self.webview = WebKit2.WebView()
        self.webview.set_settings(wk_settings)

        # Set dark background to prevent white flash on load
        bg_color = Gdk.RGBA()
        bg_color.parse("#0b1020")
        try:
            self.webview.set_background_color(bg_color)
        except Exception:
            pass

        # Scrolled container
        scrolled = Gtk.ScrolledWindow()
        scrolled.add(self.webview)
        self.add(scrolled)

        # Show splash screen while the server finishes starting
        self.webview.load_html(SPLASH_HTML, "about:blank")
        self._server_check_attempts = 0
        GLib.timeout_add(250, self._check_server_and_load)

    def _setup_shortcuts(self) -> None:
        accel_group = Gtk.AccelGroup()

        # Ctrl+R: Reload
        accel_group.connect(
            Gdk.keyval_from_name("r"),
            Gdk.ModifierType.CONTROL_MASK,
            0,
            lambda *_: self._on_reload(None),
        )
        # Ctrl+Q: Quit
        accel_group.connect(
            Gdk.keyval_from_name("q"),
            Gdk.ModifierType.CONTROL_MASK,
            0,
            lambda *_: self._on_destroy(None),
        )
        # Ctrl+=: Zoom In
        accel_group.connect(
            Gdk.keyval_from_name("equal"),
            Gdk.ModifierType.CONTROL_MASK,
            0,
            lambda *_: self._on_zoom_in(None),
        )
        # Ctrl+-: Zoom Out
        accel_group.connect(
            Gdk.keyval_from_name("minus"),
            Gdk.ModifierType.CONTROL_MASK,
            0,
            lambda *_: self._on_zoom_out(None),
        )
        # Ctrl+0: Reset Zoom
        accel_group.connect(
            Gdk.keyval_from_name("0"),
            Gdk.ModifierType.CONTROL_MASK,
            0,
            lambda *_: self._on_zoom_reset(None),
        )
        # F11: Fullscreen
        self.connect("key-press-event", self._on_key_press)

        self.add_accel_group(accel_group)

    # ── Event Handlers ────────────────────────────────────────────────────

    def _on_reload(self, _widget) -> None:
        self.webview.reload()

    def _on_zoom_in(self, _widget) -> None:
        level = self.webview.get_zoom_level()
        self.webview.set_zoom_level(min(level + 0.1, 3.0))
        self._update_zoom_label()

    def _on_zoom_out(self, _widget) -> None:
        level = self.webview.get_zoom_level()
        self.webview.set_zoom_level(max(level - 0.1, 0.3))
        self._update_zoom_label()

    def _on_zoom_reset(self, _widget) -> None:
        self.webview.set_zoom_level(1.0)
        self._update_zoom_label()

    def _update_zoom_label(self) -> None:
        pct = int(self.webview.get_zoom_level() * 100)
        self._zoom_label.set_label(f"{pct}%")

    def _on_pin_toggled(self, btn) -> None:
        self.set_keep_above(btn.get_active())

    def _on_fullscreen(self, _widget) -> None:
        if self._is_fullscreen:
            self.unfullscreen()
        else:
            self.fullscreen()

    _is_fullscreen = False

    def _on_key_press(self, _widget, event) -> bool:
        if event.keyval == Gdk.KEY_F11:
            self._on_fullscreen(None)
            return True
        return False

    def _on_delete_event(self, _widget, _event) -> bool:
        """Intercept the window close button.

        If a system-tray indicator is available, hide the window instead of
        quitting so the app keeps running in the background.
        """
        if HAS_APPINDICATOR:
            # Save window geometry before hiding
            if hasattr(self, '_state_persistence') and self._state_persistence:
                self._state_persistence.save_now()
            self.hide()
            return True  # prevent destruction
        return False  # allow normal destroy → _on_destroy

    def _on_destroy(self, _widget) -> None:
        # Save window geometry one last time
        if hasattr(self, '_state_persistence') and self._state_persistence:
            self._state_persistence.save_now()
        Gtk.main_quit()

    def _check_server_and_load(self) -> bool:
        """Periodically check if the dashboard server is reachable, then load the real URL."""
        self._server_check_attempts += 1
        try:
            # Parse host/port from self.url
            from urllib.parse import urlparse
            parsed = urlparse(self.url)
            host = parsed.hostname or "127.0.0.1"
            port = parsed.port or 80
            with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
                s.settimeout(0.5)
                s.connect((host, port))
            # Server is ready — load the real dashboard
            self.webview.load_uri(self.url)
            return False  # GLib.SOURCE_REMOVE
        except OSError:
            if self._server_check_attempts >= 40:  # ~10 s
                print("Warning: server check timed out; loading URL anyway.",
                      file=sys.stderr)
                self.webview.load_uri(self.url)
                return False  # GLib.SOURCE_REMOVE
            return True  # GLib.SOURCE_CONTINUE — try again

    # Track fullscreen state
    def do_window_state_event(self, event):
        self._is_fullscreen = bool(
            event.new_window_state & Gdk.WindowState.FULLSCREEN
        )
        return Gtk.Window.do_window_state_event(self, event)


# ─────────────────────────────────────────────────────────────────────────────
# System Tray (optional, requires AppIndicator3)
# ─────────────────────────────────────────────────────────────────────────────
def setup_tray_indicator(window: DashboardWindow, icon_path: str | None) -> None:
    """Create a system tray icon with a menu to show/hide/quit the dashboard."""
    if not HAS_APPINDICATOR:
        return

    icon = icon_path if icon_path and os.path.isfile(icon_path) else "utilities-system-monitor"

    indicator = AppIndicator3.Indicator.new(
        "resource-dashboard",
        icon,
        AppIndicator3.IndicatorCategory.SYSTEM_SERVICES,
    )
    indicator.set_status(AppIndicator3.IndicatorStatus.ACTIVE)
    indicator.set_title("Resource Dashboard")

    menu = Gtk.Menu()

    show_item = Gtk.MenuItem(label="Show Dashboard")
    show_item.connect("activate", lambda _: (window.present(), window.deiconify()))
    menu.append(show_item)

    hide_item = Gtk.MenuItem(label="Minimize to Tray")
    hide_item.connect("activate", lambda _: window.iconify())
    menu.append(hide_item)

    menu.append(Gtk.SeparatorMenuItem())

    quit_item = Gtk.MenuItem(label="Quit")
    quit_item.connect("activate", lambda _: Gtk.main_quit())
    menu.append(quit_item)

    menu.show_all()
    indicator.set_menu(menu)


# ─────────────────────────────────────────────────────────────────────────────
# Startup Splash (inline CSS notification)
# ─────────────────────────────────────────────────────────────────────────────
SPLASH_HTML = """<!doctype html>
<html><head><style>
  body {
    margin: 0; display: flex; align-items: center; justify-content: center;
    min-height: 100vh; font-family: 'Inter', system-ui, sans-serif;
    background: linear-gradient(135deg, #070b16, #0b1020, #111827);
    color: #e5eefc;
  }
  .loader {
    text-align: center; animation: fadeIn 0.5s ease;
  }
  @keyframes fadeIn { from { opacity: 0; transform: translateY(20px); } to { opacity: 1; transform: translateY(0); } }
  .spinner {
    width: 48px; height: 48px; border: 4px solid rgba(94, 234, 212, 0.2);
    border-top-color: #5eead4; border-radius: 50%;
    animation: spin 1s linear infinite; margin: 0 auto 20px;
  }
  @keyframes spin { to { transform: rotate(360deg); } }
  h2 { font-size: 1.4rem; margin: 0 0 8px; letter-spacing: -0.02em; }
  p { color: #92a2bf; font-size: 0.9rem; margin: 0; }
</style></head><body>
  <div class="loader">
    <div class="spinner"></div>
    <h2>Starting Resource Dashboard...</h2>
    <p>Initializing system monitors</p>
  </div>
</body></html>"""


# ─────────────────────────────────────────────────────────────────────────────
# Entry Point
# ─────────────────────────────────────────────────────────────────────────────
def parse_app_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Resource Dashboard – Native Linux Desktop App",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""\
Keyboard Shortcuts:
  Ctrl+R    Reload dashboard
  Ctrl+=    Zoom in
  Ctrl+-    Zoom out
  Ctrl+0    Reset zoom
  Ctrl+Q    Quit
  F11       Toggle fullscreen
""",
    )
    parser.add_argument(
        "--root",
        default=str(Path.home()),
        help="Filesystem root to monitor for disk usage (default: home directory)",
    )
    parser.add_argument(
        "--top",
        type=int,
        default=DEFAULT_TOP_PROCESSES,
        help="Number of top processes to display (default: 8)",
    )
    parser.add_argument(
        "--zoom",
        type=float,
        default=1.0,
        help="Initial zoom level (default: 1.0)",
    )
    parser.add_argument(
        "--host",
        default="127.0.0.1",
        help="Host IP to bind the server to (use 0.0.0.0 for network access). Default: 127.0.0.1",
    )
    return parser.parse_args()


def main() -> None:
    # Handle SIGINT gracefully
    signal.signal(signal.SIGINT, lambda *_: Gtk.main_quit())

    args = parse_app_args()
    sample_root = Path(args.root).expanduser().resolve()
    if not sample_root.exists():
        print(f"Error: Path does not exist: {sample_root}", file=sys.stderr)
        sys.exit(1)

    # Find a free port and start the server
    port = find_free_port()
    host = args.host
    url = f"http://{host}:{port}/"
    if host == "0.0.0.0":
        url = f"http://127.0.0.1:{port}/"

    server_thread = DashboardServerThread(host, port, sample_root, args.top)
    server_thread.start()

    # Wait for the server to be ready (with retries)
    wait_for_server(host, port, timeout=10)

    # ── Resolve icon path ──
    icon_path = None
    candidates = [
        SCRIPT_DIR / "resource_dashboard_icon.svg",
        SCRIPT_DIR / "resource_dashboard_icon.png",
        Path.home() / ".local" / "share" / "icons" / "resource-dashboard.svg",
        Path.home() / ".local" / "share" / "icons" / "resource-dashboard.png",
        SCRIPT_DIR / "icon.png",
    ]
    for candidate in candidates:
        if candidate.is_file():
            icon_path = str(candidate)
            break

    # ── Create the GTK window ──
    window = DashboardWindow(url, icon_path)
    if args.zoom != 1.0:
        window.webview.set_zoom_level(args.zoom)
        window._update_zoom_label()

    # ── Window state persistence ──
    window._state_persistence = WindowStatePersistence(window)

    # ── System tray (if available) ──
    setup_tray_indicator(window, icon_path)

    window.show_all()

    print(f"Dashboard server running at {url}")
    print(f"Monitoring: {sample_root}")
    print("Press Ctrl+Q to quit.")

    Gtk.main()

    # Cleanup
    server_thread.stop()
    print("\nDashboard closed.")


if __name__ == "__main__":
    main()
