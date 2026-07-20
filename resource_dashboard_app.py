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
import urllib.error
import urllib.request
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

    def __init__(
        self,
        window: Gtk.Window,
        debounce_ms: int = 500,
        config_file: Path | None = None,
        default_size: tuple[int, int] = (1400, 900),
    ):
        self._window = window
        self._debounce_ms = debounce_ms
        self._save_timer_id: int | None = None
        self._config_file = config_file or self.CONFIG_FILE
        self._default_size = default_size
        self.last_state: dict = {}
        self._restore()
        window.connect("configure-event", self._on_configure)

    # ── Restore ───────────────────────────────────────────────────────────
    def _restore(self) -> None:
        """Load previously-saved geometry and apply it (with screen-bounds check)."""
        if not self._config_file.is_file():
            return
        try:
            state = json.loads(self._config_file.read_text())
        except (json.JSONDecodeError, OSError):
            return
        self.last_state = state

        width = state.get("width", self._default_size[0])
        height = state.get("height", self._default_size[1])
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
        state = dict(self.last_state)
        state.update({
            "width": size.width,
            "height": size.height,
            "x": pos.root_x,
            "y": pos.root_y,
        })
        self.last_state = state
        try:
            self._config_file.parent.mkdir(parents=True, exist_ok=True)
            self._config_file.write_text(json.dumps(state, indent=2))
        except OSError as exc:
            print(f"Warning: could not save window state: {exc}", file=sys.stderr)
        return False  # GLib.SOURCE_REMOVE

    def save_now(self, extra: dict | None = None) -> None:
        """Immediately persist the current window geometry (call before hide/quit).

        *extra* lets callers stash additional fields (e.g. visibility) in the
        same state file without a separate config file.
        """
        if extra:
            self.last_state.update(extra)
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

        # Mini widget toggle (small always-on-top HUD; visible while minimized)
        mini_btn = Gtk.ToggleButton(label="▢")
        mini_btn.set_tooltip_text("Mini Widget — stays visible when minimized (Ctrl+M)")
        self._mini_toggle_handler_id = mini_btn.connect("toggled", self._on_mini_toggled)
        self._mini_btn = mini_btn
        header.pack_end(mini_btn)

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
        # Ctrl+M: Toggle mini widget
        accel_group.connect(
            Gdk.keyval_from_name("m"),
            Gdk.ModifierType.CONTROL_MASK,
            0,
            lambda *_: self._mini_btn.set_active(not self._mini_btn.get_active()),
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

    # ── Mini widget ──────────────────────────────────────────────────────
    _mini_widget = None

    def set_mini_widget(self, widget: "MiniWidgetWindow") -> None:
        """Attach the mini-widget instance and keep the header toggle in sync
        with it in both directions (button click, or the widget's own ✕)."""
        self._mini_widget = widget
        widget.set_visibility_callback(self._sync_mini_button)
        self._sync_mini_button()

    def _sync_mini_button(self) -> None:
        active = bool(self._mini_widget and self._mini_widget.get_visible())
        self._mini_btn.handler_block(self._mini_toggle_handler_id)
        self._mini_btn.set_active(active)
        self._mini_btn.handler_unblock(self._mini_toggle_handler_id)

    def _on_mini_toggled(self, btn) -> None:
        if not self._mini_widget:
            return
        if btn.get_active():
            self._mini_widget.show_widget()
        else:
            self._mini_widget.hide_widget()

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
        if self._mini_widget:
            self._mini_widget.shutdown()
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
# Mini Widget: a small always-on-top HUD card for glancing at usage while
# working in another application (e.g. VS Code), without switching windows.
# ─────────────────────────────────────────────────────────────────────────────
MINI_WIDGET_CSS = b"""
window.mini-widget {
  background-color: rgba(11, 16, 32, 0.94);
  border-radius: 14px;
  border: 1px solid rgba(148, 163, 184, 0.25);
}
.mini-title {
  color: #e5eefc; font-weight: 700; font-size: 11px; letter-spacing: 0.02em;
}
.mini-updated {
  color: #64748b; font-size: 9px;
}
.mini-label {
  color: #92a2bf; font-size: 10px; font-weight: 600; min-width: 44px;
}
.mini-value {
  color: #e5eefc; font-weight: 700; font-size: 10px; min-width: 40px;
}
button.mini-btn {
  background: transparent; border: none; color: #92a2bf; padding: 1px 3px;
  min-width: 0; min-height: 0;
}
button.mini-btn:hover { color: #e5eefc; background: rgba(148, 163, 184, 0.12); border-radius: 6px; }
progressbar.mini-bar trough {
  background-color: rgba(148, 163, 184, 0.16); border-radius: 999px; min-height: 6px; border: none;
}
progressbar.mini-bar progress {
  background-color: #5eead4; background-image: none; border-radius: 999px; min-height: 6px;
}
progressbar.mini-bar.warn progress { background-color: #fbbf24; }
progressbar.mini-bar.danger progress { background-color: #fb7185; }
"""


class MiniWidgetWindow(Gtk.Window):
    """A small, undecorated, always-on-top card showing live CPU/Mem/GPU/Disk.

    Polls the same local dashboard HTTP server the main window uses, over a
    plain socket request (no WebKit), so it stays cheap enough to leave open
    all the time. It is a separate top-level window, so minimizing the main
    DashboardWindow does not affect it.
    """

    CONFIG_FILE = Path.home() / ".config" / "resource-dashboard" / "mini_widget_state.json"
    POLL_SECONDS = 2.0

    def __init__(self, metrics_url: str, on_restore_main):
        super().__init__(type=Gtk.WindowType.TOPLEVEL)
        self._metrics_url = metrics_url
        self._on_restore_main = on_restore_main
        self._on_visibility_change = None
        self._poll_thread: threading.Thread | None = None
        self._stop_event = threading.Event()

        self.set_title("Resource Mini")
        self.set_decorated(False)
        self.set_resizable(False)
        self.set_default_size(210, 190)
        self.set_keep_above(True)
        self.set_skip_taskbar_hint(True)
        self.set_skip_pager_hint(True)
        self.set_type_hint(Gdk.WindowTypeHint.UTILITY)
        self.get_style_context().add_class("mini-widget")

        screen = self.get_screen()
        visual = screen.get_rgba_visual() if screen else None
        if visual:
            self.set_visual(visual)
        self.set_app_paintable(True)

        self.connect("delete-event", self._on_delete)
        self.connect("button-press-event", self._on_drag)

        self._build_ui()
        self._state = WindowStatePersistence(
            self, config_file=self.CONFIG_FILE, default_size=(210, 190)
        )
        if "x" not in self._state.last_state:
            self._default_position()

    # ── UI construction ─────────────────────────────────────────────────
    def _build_ui(self) -> None:
        outer = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=6)
        outer.set_margin_top(10)
        outer.set_margin_bottom(10)
        outer.set_margin_start(12)
        outer.set_margin_end(12)
        self.add(outer)

        header = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=4)
        title = Gtk.Label(label="🖥️ Resource Mini")
        title.get_style_context().add_class("mini-title")
        title.set_halign(Gtk.Align.START)
        header.pack_start(title, True, True, 0)

        restore_btn = Gtk.Button(label="⤢")
        restore_btn.get_style_context().add_class("mini-btn")
        restore_btn.set_tooltip_text("Show main window")
        restore_btn.connect("clicked", lambda _b: self._on_restore_main())
        header.pack_end(restore_btn, False, False, 0)

        close_btn = Gtk.Button(label="✕")
        close_btn.get_style_context().add_class("mini-btn")
        close_btn.set_tooltip_text("Hide mini widget")
        close_btn.connect("clicked", lambda _b: self.hide_widget())
        header.pack_end(close_btn, False, False, 0)

        outer.pack_start(header, False, False, 0)

        self._rows = {}
        for key, label in (("cpu", "CPU"), ("mem", "MEM"), ("gpu", "GPU"), ("disk", "DISK")):
            row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=6)
            name_lbl = Gtk.Label(label=label)
            name_lbl.get_style_context().add_class("mini-label")
            name_lbl.set_halign(Gtk.Align.START)
            row.pack_start(name_lbl, False, False, 0)

            bar = Gtk.ProgressBar()
            bar.get_style_context().add_class("mini-bar")
            bar.set_valign(Gtk.Align.CENTER)
            row.pack_start(bar, True, True, 0)

            value_lbl = Gtk.Label(label="—")
            value_lbl.get_style_context().add_class("mini-value")
            value_lbl.set_halign(Gtk.Align.END)
            row.pack_start(value_lbl, False, False, 0)

            outer.pack_start(row, False, False, 0)
            self._rows[key] = (row, bar, value_lbl)

        self._updated_lbl = Gtk.Label(label="Waiting for data…")
        self._updated_lbl.get_style_context().add_class("mini-updated")
        self._updated_lbl.set_halign(Gtk.Align.START)
        outer.pack_start(self._updated_lbl, False, False, 0)

    def _default_position(self) -> None:
        screen = self.get_screen()
        if not screen:
            return
        monitor = screen.get_display().get_monitor(0)
        geo = monitor.get_workarea() if monitor else None
        if geo:
            self.move(geo.x + geo.width - 230, geo.y + geo.height - 210)

    def _on_drag(self, widget, event) -> bool:
        if event.button == 1:
            self.begin_move_drag(event.button, int(event.x_root), int(event.y_root), event.time)
        return False

    def _on_delete(self, *_args) -> bool:
        self.hide_widget()
        return True  # prevent destruction; this window is reused for the app's lifetime

    # ── Visibility / polling lifecycle ──────────────────────────────────
    def set_visibility_callback(self, callback) -> None:
        """Callback invoked (no args) whenever show_widget()/hide_widget() runs,
        so the main window's toggle button can stay in sync when the mini
        widget is closed via its own ✕ button."""
        self._on_visibility_change = callback

    def show_widget(self) -> None:
        self.show_all()
        self.present()
        if self._poll_thread is None or not self._poll_thread.is_alive():
            self._stop_event.clear()
            self._poll_thread = threading.Thread(target=self._poll_loop, daemon=True)
            self._poll_thread.start()
        self._state.save_now(extra={"visible": True})
        if self._on_visibility_change:
            self._on_visibility_change()

    def hide_widget(self) -> None:
        self._stop_event.set()
        self.hide()
        self._state.save_now(extra={"visible": False})
        if self._on_visibility_change:
            self._on_visibility_change()

    def shutdown(self) -> None:
        self._stop_event.set()

    # ── Data polling (background thread) ────────────────────────────────
    def _poll_loop(self) -> None:
        while not self._stop_event.is_set():
            try:
                with urllib.request.urlopen(self._metrics_url, timeout=3) as resp:
                    data = json.loads(resp.read().decode("utf-8"))
                GLib.idle_add(self._apply_metrics, data)
            except (urllib.error.URLError, OSError, ValueError, json.JSONDecodeError):
                GLib.idle_add(self._apply_connection_error)
            self._stop_event.wait(self.POLL_SECONDS)

    def _set_row(self, key: str, percent: float | None, extra_text: str = "") -> None:
        row, bar, value_lbl = self._rows[key]
        if percent is None:
            row.set_visible(False)
            return
        row.set_visible(True)
        fraction = max(0.0, min(1.0, percent / 100.0))
        bar.set_fraction(fraction)
        ctx = bar.get_style_context()
        ctx.remove_class("warn")
        ctx.remove_class("danger")
        if percent >= 90:
            ctx.add_class("danger")
        elif percent >= 75:
            ctx.add_class("warn")
        value_lbl.set_label(f"{percent:.0f}%{extra_text}")

    def _apply_metrics(self, data: dict) -> bool:
        self._set_row("cpu", float(data.get("cpu_percent", 0) or 0))
        mem = data.get("memory") or {}
        self._set_row("mem", float(mem.get("percent", 0) or 0))
        disk = data.get("disk") or {}
        self._set_row("disk", float(disk.get("percent", 0) or 0))

        gpus = data.get("gpu") or []
        if gpus:
            utils = [float(g["utilization"]) for g in gpus if str(g.get("utilization")) not in ("N/A", "[N/A]", "")]
            avg_util = sum(utils) / len(utils) if utils else 0.0
            self._set_row("gpu", avg_util)
        else:
            self._set_row("gpu", None)

        self._updated_lbl.set_label(f"Updated {time.strftime('%H:%M:%S')}")
        return False  # GLib.SOURCE_REMOVE

    def _apply_connection_error(self) -> bool:
        self._updated_lbl.set_label("Connection lost — retrying…")
        return False  # GLib.SOURCE_REMOVE


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

    mini_item = Gtk.MenuItem(label="Toggle Mini Widget")
    mini_item.connect("activate", lambda _: window._mini_btn.set_active(not window._mini_btn.get_active()))
    menu.append(mini_item)

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
  Ctrl+M    Toggle mini widget (small always-on-top HUD)
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

    # ── Shared CSS (used by the mini widget) ──
    css_provider = Gtk.CssProvider()
    css_provider.load_from_data(MINI_WIDGET_CSS)
    Gtk.StyleContext.add_provider_for_screen(
        Gdk.Screen.get_default(), css_provider, Gtk.STYLE_PROVIDER_PRIORITY_APPLICATION
    )

    # ── Create the GTK window ──
    window = DashboardWindow(url, icon_path)
    if args.zoom != 1.0:
        window.webview.set_zoom_level(args.zoom)
        window._update_zoom_label()

    # ── Window state persistence ──
    window._state_persistence = WindowStatePersistence(window)

    # ── Mini widget: small always-on-top HUD, independent of the main window's
    #    minimize state, so usage stays visible while working in another app ──
    metrics_url = f"http://127.0.0.1:{port}/api/metrics"
    mini_widget = MiniWidgetWindow(
        metrics_url, on_restore_main=lambda: (window.present(), window.deiconify())
    )
    if mini_widget._state.last_state.get("visible"):
        mini_widget.show_widget()
    window.set_mini_widget(mini_widget)

    # ── System tray (if available) ──
    setup_tray_indicator(window, icon_path)

    window.show_all()

    print(f"Dashboard server running at {url}")
    print(f"Monitoring: {sample_root}")
    print("Press Ctrl+Q to quit. Ctrl+M toggles the mini widget.")

    Gtk.main()

    # Cleanup
    mini_widget.shutdown()
    server_thread.stop()
    print("\nDashboard closed.")


if __name__ == "__main__":
    main()
