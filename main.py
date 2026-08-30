#!/usr/bin/env python3
"""
AutoFlow
========
A small desktop app for running your own drop-in Python macros.

- Put .py files (each defining `def run(stop_event): ...`) into macros/
- Pick one, hit Start (or press F8) to run it
- Press F8 again (or hit Stop) to stop it
- Dashboard shows live stats, Macros page runs things, History tracks past
  runs, Settings lets you change the hotkey.

Run it with:  python main.py
"""

import ctypes
import importlib.util
import json
import os
import queue
import subprocess
import sys
import threading
import time
import tkinter as tk
from tkinter import messagebox, simpledialog

try:
    import customtkinter as ctk
except ImportError:
    print("Missing dependency 'customtkinter'. Install it with:\n    pip install customtkinter")
    sys.exit(1)

try:
    import pystray
    from PIL import Image as PILImage
    TRAY_AVAILABLE = True
except ImportError:
    TRAY_AVAILABLE = False

if getattr(sys, "frozen", False):
    BASE_DIR = os.path.dirname(os.path.abspath(sys.executable))
else:
    BASE_DIR = os.path.dirname(os.path.abspath(__file__))
MACROS_DIR = os.path.join(BASE_DIR, "macros")
CONFIG_PATH = os.path.join(BASE_DIR, "config.json")
HISTORY_PATH = os.path.join(BASE_DIR, "history.json")
HISTORY_LIMIT = 200

DEFAULT_CONFIG = {"hotkey": "f8", "last_selected": None}

AI_MACRO_PROMPT = """I'm using a desktop app called AutoFlow that runs Python macros. Please write a macro for the task I describe below.

Requirements for the macro file:
- It must define exactly one function: def run(stop_event):
- `stop_event` is a threading.Event. Check it periodically so the macro can be stopped cleanly by the app — never loop forever without checking it.
- Use the sleep() helper instead of raw time.sleep(), so Stop actually works:
      from macro_api import sleep
      if sleep(1, stop_event):   # waits 1 second, returns True if the user asked to stop early
          return
- Anything printed with print() shows up in the app's log panel — add helpful print() statements so I can see what it's doing.
- If you need pyautogui (for clicking/typing) or any other optional library, import it INSIDE run(), not at the top of the file, and print a friendly message if the import fails (e.g. "Run: pip install pyautogui").
- Output ONLY the complete Python file content, ready to save as a .py file — no extra explanation before or after.

Here's the minimal expected structure:

    from macro_api import sleep

    def run(stop_event):
        print("Macro started")
        for i in range(10):
            if sleep(1, stop_event):
                print("Stopped early")
                return
            print(i)
        print("Macro finished")

Now write the macro for this task:
[DESCRIBE WHAT YOU WANT THE MACRO TO DO HERE]
"""

# Single-instance lock: binding a localhost socket doubles as a lock (only one
# process can hold it) and a way for a second launch to signal the first one
# to come to the front, instead of just failing silently.
SINGLE_INSTANCE_PORT = 51834


def _try_acquire_single_instance_lock():
    """Returns a bound, listening socket if this is the first instance. If
    another instance is already running, pings it to raise its window and
    returns None."""
    import socket
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        s.bind(("127.0.0.1", SINGLE_INSTANCE_PORT))
        s.listen(5)
        return s
    except OSError:
        try:
            client = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            client.settimeout(1)
            client.connect(("127.0.0.1", SINGLE_INSTANCE_PORT))
            client.sendall(b"show")
            client.close()
        except Exception:
            pass
        s.close()
        return None


# ---------------------------------------------------------------------------
# Theme — near-black background, #00FFBF mint/teal accent glow throughout
# ---------------------------------------------------------------------------
BG = "#030405"
PANEL = "#07090a"
CARD = "#0b0d0f"
CARD_HOVER = "#0f1614"
CARD_SELECTED = "#0c2620"
BORDER = "#123a33"
BORDER_STRONG = "#1c5a4e"
TEXT = "#eafff8"
TEXT_DIM = "#5c7a73"

ACCENT = "#00FFBF"
ACCENT_HOVER = "#4dffd4"
ACCENT_DIM = "#0a5c48"
GREEN = "#00FFBF"
RED = "#FF2E63"
RED_HOVER = "#ff5c82"
RED_DIM = "#3d0f1f"
YELLOW = "#00FFBF"

FONT = "Consolas"
FONT_MONO = "Consolas"

ctk.set_appearance_mode("dark")
ctk.set_widget_scaling(1.0)


def load_config():
    if os.path.exists(CONFIG_PATH):
        try:
            with open(CONFIG_PATH, "r", encoding="utf-8") as f:
                cfg = json.load(f)
            merged = dict(DEFAULT_CONFIG)
            merged.update(cfg)
            return merged
        except Exception:
            pass
    return dict(DEFAULT_CONFIG)


def save_config(cfg):
    try:
        with open(CONFIG_PATH, "w", encoding="utf-8") as f:
            json.dump(cfg, f, indent=2)
    except Exception:
        pass


def load_history():
    if os.path.exists(HISTORY_PATH):
        try:
            with open(HISTORY_PATH, "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception:
            pass
    return []


def save_history(history):
    try:
        with open(HISTORY_PATH, "w", encoding="utf-8") as f:
            json.dump(history[-HISTORY_LIMIT:], f, indent=2)
    except Exception:
        pass


class QueueWriter:
    """Redirects print() output into a thread-safe queue the GUI polls."""

    def __init__(self, q):
        self.q = q

    def write(self, text):
        if text:
            self.q.put(text)

    def flush(self):
        pass


class MacroRow(ctk.CTkFrame):
    """A single clickable row in the macro list."""

    def __init__(self, master, name, on_click):
        super().__init__(master, fg_color=CARD, corner_radius=10, height=48, cursor="hand2",
                          border_width=1, border_color=BORDER)
        self.name = name
        self.on_click = on_click
        self.selected = False
        self.status = "stopped"
        self.grid_propagate(False)
        self.columnconfigure(1, weight=1)

        self.dot = ctk.CTkLabel(self, text="●", text_color=TEXT_DIM, font=(FONT, 13), width=18)
        self.dot.grid(row=0, column=0, padx=(14, 4), pady=10)

        self.name_label = ctk.CTkLabel(self, text=name, text_color=TEXT, font=(FONT, 13), anchor="w")
        self.name_label.grid(row=0, column=1, sticky="w", pady=10)

        self.status_label = ctk.CTkLabel(self, text="idle", text_color=TEXT_DIM, font=(FONT, 11))
        self.status_label.grid(row=0, column=2, padx=(4, 14), pady=10)

        for widget in (self, self.dot, self.name_label, self.status_label):
            widget.bind("<Button-1>", self._click)
            widget.bind("<Enter>", self._enter)
            widget.bind("<Leave>", self._leave)

    def _click(self, _e=None):
        self.on_click(self.name)

    def _enter(self, _e=None):
        if not self.selected:
            self.configure(fg_color=CARD_HOVER, border_color=BORDER_STRONG)

    def _leave(self, _e=None):
        if not self.selected:
            self.configure(fg_color=CARD, border_color=BORDER)

    def set_selected(self, selected):
        self.selected = selected
        self.configure(fg_color=CARD_SELECTED if selected else CARD,
                        border_color=ACCENT if selected else BORDER)

    def set_status(self, status):
        self.status = status
        if status == "running":
            self.dot.configure(text_color=GREEN)
            self.status_label.configure(text="running", text_color=GREEN)
        else:
            self.dot.configure(text_color=TEXT_DIM)
            self.status_label.configure(text="idle", text_color=TEXT_DIM)


def fmt_duration(seconds):
    seconds = max(0, int(seconds))
    if seconds < 60:
        return f"{seconds}s"
    m, s = divmod(seconds, 60)
    if m < 60:
        return f"{m}m {s}s"
    h, m = divmod(m, 60)
    return f"{h}h {m}m"


def fmt_when(ts):
    return time.strftime("%b %d, %H:%M", time.localtime(ts))


class Card(ctk.CTkFrame):
    """Reusable rounded, bordered panel used across every page."""

    def __init__(self, master, **kwargs):
        kwargs.setdefault("fg_color", PANEL)
        kwargs.setdefault("corner_radius", 16)
        kwargs.setdefault("border_width", 1)
        kwargs.setdefault("border_color", BORDER_STRONG)
        super().__init__(master, **kwargs)


class AutoFlowApp(ctk.CTk):
    def __init__(self):
        super().__init__()
        self.title("AutoFlow")
        self.geometry("1080x660")
        self.minsize(860, 540)
        self.configure(fg_color=BG)

        self.cfg = load_config()
        self.history = load_history()
        self.macros = {}
        self.rows = {}
        self.selected_name = None
        self.status = "stopped"
        self.run_thread = None
        self.stop_event = threading.Event()
        self.log_queue = queue.Queue()
        self._orig_stdout = sys.stdout
        self.hotkey_lib_ok = False
        self._active_hotkey = None    # whichever hotkey string is genuinely registered right now
        self._local_bind_seq = None   # the Tk-level binding sequence, kept in sync with _active_hotkey
        self.current_run = None       # {"name": ..., "start": ts}
        self._last_run_error = None
        self._status_listeners = []   # callables(status) -> None
        self._selection_listeners = []  # callables(name) -> None

        self._set_app_icon()
        self._build_shell()
        self.refresh_macros()
        self._register_hotkey()
        self._set_status("stopped")
        self.show_page("dashboard")

        self.after(100, self._poll_log_queue)
        self.after(150, self._poll_thread_status)
        self.after(300, self._set_app_icon)  # customtkinter can reset the icon shortly after startup
        self.protocol("WM_DELETE_WINDOW", self._on_close)

        self.tray_icon = None
        self._setup_tray()

    def _setup_tray(self):
        if not TRAY_AVAILABLE:
            return
        png_path = os.path.join(BASE_DIR, "assets", "icon.png")
        try:
            image = PILImage.open(png_path).convert("RGBA") if os.path.exists(png_path) else PILImage.new(
                "RGBA", (64, 64), (0, 255, 191, 255))
        except Exception:
            image = PILImage.new("RGBA", (64, 64), (0, 255, 191, 255))

        menu = pystray.Menu(
            pystray.MenuItem("Show AutoFlow", lambda: self.after(0, self._restore_window), default=True),
            pystray.MenuItem("Quit AutoFlow", lambda: self.after(0, self._quit_app)),
        )
        self.tray_icon = pystray.Icon("AutoFlow", image, "AutoFlow", menu)
        threading.Thread(target=self.tray_icon.run, daemon=True).start()

    def _restore_window(self):
        self.deiconify()
        self.lift()
        self.focus_force()

    def _start_single_instance_listener(self, sock):
        self._single_instance_sock = sock

        def worker():
            while True:
                try:
                    conn, _ = sock.accept()
                    conn.recv(64)
                    conn.close()
                    self.after(0, self._restore_window)
                except OSError:
                    break  # socket was closed (app quitting)

        threading.Thread(target=worker, daemon=True).start()

    def _quit_app(self):
        if self.status == "running":
            self.stop_event.set()
        save_config(self.cfg)
        if self.tray_icon is not None:
            try:
                self.tray_icon.stop()
            except Exception:
                pass
        sock = getattr(self, "_single_instance_sock", None)
        if sock is not None:
            try:
                sock.close()
            except Exception:
                pass
        self.destroy()

    def _set_app_icon(self):
        ico_path = os.path.join(BASE_DIR, "assets", "icon.ico")
        png_path = os.path.join(BASE_DIR, "assets", "icon.png")
        try:
            if sys.platform.startswith("win") and os.path.exists(ico_path):
                self.iconbitmap(ico_path)
            elif os.path.exists(png_path):
                self._icon_img = tk.PhotoImage(file=png_path)
                self.iconphoto(True, self._icon_img)
        except Exception:
            pass

    # ============================================================ SHELL UI
    def _build_shell(self):
        root = ctk.CTkFrame(self, fg_color=BG)
        root.pack(fill="both", expand=True)
        root.columnconfigure(1, weight=1)
        root.rowconfigure(0, weight=1)

        self._build_sidebar(root)

        body = ctk.CTkFrame(root, fg_color=BG)
        body.grid(row=0, column=1, sticky="nsew", padx=20, pady=20)
        body.columnconfigure(0, weight=1)
        body.rowconfigure(1, weight=1)

        self._build_topbar(body)

        self.page_container = ctk.CTkFrame(body, fg_color="transparent")
        self.page_container.grid(row=1, column=0, sticky="nsew")
        self.page_container.columnconfigure(0, weight=1)
        self.page_container.rowconfigure(0, weight=1)

        self.pages = {}
        for key, builder in (
            ("dashboard", self._build_dashboard_page),
            ("macros", self._build_macros_page),
            ("history", self._build_history_page),
            ("settings", self._build_settings_page),
        ):
            page = ctk.CTkFrame(self.page_container, fg_color="transparent")
            page.grid(row=0, column=0, sticky="nsew")
            builder(page)
            self.pages[key] = page

    def _build_sidebar(self, root):
        sidebar = ctk.CTkFrame(root, fg_color=PANEL, corner_radius=0, width=84,
                                border_width=0)
        sidebar.grid(row=0, column=0, sticky="nsw")
        sidebar.grid_propagate(False)

        badge = ctk.CTkFrame(sidebar, fg_color="transparent")
        badge.pack(pady=(24, 30))
        ctk.CTkLabel(badge, text="⬡", text_color=ACCENT, font=(FONT, 30, "bold")).pack()

        self.nav_buttons = {}
        nav_items = [
            ("dashboard", "⌂", "Dashboard"),
            ("macros", "▶", "Macros"),
            ("history", "◷", "History"),
            ("settings", "⚙", "Settings"),
        ]
        for key, glyph, tooltip in nav_items:
            btn = ctk.CTkButton(
                sidebar, text=glyph, width=52, height=52, corner_radius=14,
                fg_color="transparent", hover_color=CARD_HOVER, text_color=TEXT_DIM,
                font=(FONT, 20), command=lambda k=key: self.show_page(k),
            )
            btn.pack(pady=6)
            self.nav_buttons[key] = btn

    def _build_topbar(self, body):
        header = ctk.CTkFrame(body, fg_color="transparent")
        header.grid(row=0, column=0, sticky="ew", pady=(0, 22))
        header.columnconfigure(1, weight=1)

        title_frame = ctk.CTkFrame(header, fg_color="transparent")
        title_frame.grid(row=0, column=0, sticky="w")
        wordmark = ctk.CTkFrame(title_frame, fg_color="transparent")
        wordmark.pack(anchor="w")
        for i, ch in enumerate("AUTOFLOW"):
            color = ACCENT if i == 3 else TEXT  # only the O after "AUT" is teal
            ctk.CTkLabel(wordmark, text=ch, text_color=color, font=("Arial Black", 50)).pack(
                side="left", padx=(0 if i == 0 else 3, 0))
        ctk.CTkFrame(title_frame, fg_color=ACCENT, height=3, corner_radius=2).pack(fill="x", pady=(2, 0))
        self.page_subtitle = ctk.CTkLabel(title_frame, text="Overview of your automations",
                                           text_color=TEXT_DIM, font=(FONT, 12))
        self.page_subtitle.pack(anchor="w", pady=(6, 0))

        self.hotkey_badge = ctk.CTkLabel(
            header, text="F8", fg_color=CARD, text_color=YELLOW, font=(FONT, 12, "bold"),
            corner_radius=8, width=36, height=28, border_width=1, border_color=BORDER,
        )
        self.hotkey_badge.grid(row=0, column=1, sticky="e", padx=(0, 10))
        self.hotkey_desc = ctk.CTkLabel(header, text="toggle start/stop", text_color=TEXT_DIM, font=(FONT, 12))
        self.hotkey_desc.grid(row=0, column=2, sticky="e")

    def show_page(self, key):
        subtitles = {
            "dashboard": "Overview of your automations",
            "macros": "Pick a macro, run it, watch the log",
            "history": "Every run, tracked",
            "settings": "Hotkey, folders, and preferences",
        }
        self.page_subtitle.configure(text=subtitles.get(key, ""))
        for k, btn in self.nav_buttons.items():
            active = k == key
            btn.configure(fg_color=CARD_SELECTED if active else "transparent",
                          text_color=ACCENT if active else TEXT_DIM)
        self.pages[key].tkraise()
        if key == "history":
            self._refresh_history_list()
        if key == "dashboard":
            self._refresh_dashboard()

    # ========================================================== DASHBOARD
    def _build_dashboard_page(self, page):
        page.columnconfigure((0, 1, 2), weight=1)
        page.rowconfigure(2, weight=1)

        # stat cards
        self.stat_macros = self._make_stat_card(page, 0, "MACROS LOADED", "0")
        self.stat_runs_today = self._make_stat_card(page, 1, "RUNS TODAY", "0")
        self.stat_current = self._make_stat_card(page, 2, "STATUS", "IDLE")

        # quick start card
        quick = Card(page)
        quick.grid(row=1, column=0, columnspan=3, sticky="ew", pady=(16, 16))
        quick.columnconfigure(0, weight=1)
        inner = ctk.CTkFrame(quick, fg_color="transparent")
        inner.grid(row=0, column=0, sticky="ew", padx=20, pady=18)
        inner.columnconfigure(0, weight=1)

        ctk.CTkLabel(inner, text="QUICK START", text_color=TEXT_DIM, font=(FONT, 11, "bold")).grid(
            row=0, column=0, sticky="w")
        self.dash_selected_label = ctk.CTkLabel(inner, text="No macro selected", text_color=TEXT,
                                                 font=(FONT, 18, "bold"), anchor="w")
        self.dash_selected_label.grid(row=1, column=0, sticky="w", pady=(2, 12))

        btn_row = ctk.CTkFrame(inner, fg_color="transparent")
        btn_row.grid(row=2, column=0, sticky="w")
        dash_start = ctk.CTkButton(
            btn_row, text="▶  Start", corner_radius=10, height=38, width=120,
            fg_color=ACCENT, hover_color=ACCENT_HOVER, text_color="#00120d",
            text_color_disabled="#4f8f7c", font=(FONT, 13, "bold"), command=self.start_macro,
        )
        dash_start.pack(side="left")
        dash_stop = ctk.CTkButton(
            btn_row, text="■  Stop", corner_radius=10, height=38, width=110,
            fg_color=RED, hover_color=RED_HOVER, text_color="#2b000a",
            text_color_disabled="#a34a5c", font=(FONT, 13, "bold"), command=self.stop_macro, state="disabled",
        )
        dash_stop.pack(side="left", padx=10)
        ctk.CTkButton(
            btn_row, text="Go to Macros →", corner_radius=10, height=38,
            fg_color="transparent", hover_color=CARD_HOVER, text_color=TEXT_DIM,
            border_width=1, border_color=BORDER, font=(FONT, 12),
            command=lambda: self.show_page("macros"),
        ).pack(side="left")

        def quick_status_cb(status):
            if status == "running":
                dash_start.configure(state="disabled", fg_color=ACCENT_DIM)
                dash_stop.configure(state="normal", fg_color=RED)
                self.stat_current.configure(text="RUNNING", text_color=GREEN)
            else:
                dash_start.configure(state="normal", fg_color=ACCENT)
                dash_stop.configure(state="disabled", fg_color=RED_DIM)
                self.stat_current.configure(text="IDLE", text_color=TEXT_DIM)
        self._status_listeners.append(quick_status_cb)

        def quick_select_cb(name):
            self.dash_selected_label.configure(text=name or "No macro selected")
        self._selection_listeners.append(quick_select_cb)

        # recent activity card
        activity = Card(page)
        activity.grid(row=2, column=0, columnspan=3, sticky="nsew")
        activity.columnconfigure(0, weight=1)
        activity.rowconfigure(1, weight=1)
        act_header = ctk.CTkFrame(activity, fg_color="transparent")
        act_header.grid(row=0, column=0, sticky="ew", padx=20, pady=(16, 8))
        act_header.columnconfigure(0, weight=1)
        ctk.CTkLabel(act_header, text="RECENT ACTIVITY", text_color=TEXT_DIM,
                     font=(FONT, 11, "bold")).grid(row=0, column=0, sticky="w")
        ctk.CTkButton(
            act_header, text="View all →", width=80, height=26, corner_radius=8,
            fg_color="transparent", hover_color=CARD_HOVER, text_color=TEXT_DIM, font=(FONT, 11),
            command=lambda: self.show_page("history"),
        ).grid(row=0, column=1, sticky="e")

        self.dash_activity_list = ctk.CTkScrollableFrame(activity, fg_color="transparent")
        self.dash_activity_list.grid(row=1, column=0, sticky="nsew", padx=14, pady=(0, 14))
        self.dash_activity_list.columnconfigure(0, weight=1)
        self.dash_activity_empty = ctk.CTkLabel(self.dash_activity_list, text="Nothing run yet — pick a macro and hit Start.",
                                                 text_color=TEXT_DIM, font=(FONT, 12))

    def _make_stat_card(self, page, col, label, value):
        card = Card(page)
        card.grid(row=0, column=col, sticky="ew", padx=(0 if col == 0 else 8, 8 if col < 2 else 0))
        ctk.CTkLabel(card, text=label, text_color=TEXT_DIM, font=(FONT, 11, "bold")).pack(
            anchor="w", padx=18, pady=(16, 2))
        value_label = ctk.CTkLabel(card, text=value, text_color=ACCENT, font=(FONT, 30, "bold"))
        value_label.pack(anchor="w", padx=18, pady=(0, 16))
        return value_label

    def _refresh_dashboard(self):
        self.stat_macros.configure(text=str(len(self.macros)))
        today = time.strftime("%Y-%m-%d")
        runs_today = sum(1 for h in self.history if time.strftime("%Y-%m-%d", time.localtime(h["start"])) == today)
        self.stat_runs_today.configure(text=str(runs_today))
        if self.status == "running":
            self.stat_current.configure(text="RUNNING", text_color=GREEN)
        else:
            self.stat_current.configure(text="IDLE", text_color=TEXT_DIM)

        for w in self.dash_activity_list.winfo_children():
            w.destroy()
        recent = list(reversed(self.history[-6:]))
        if not recent:
            self.dash_activity_empty = ctk.CTkLabel(self.dash_activity_list,
                                                      text="Nothing run yet — pick a macro and hit Start.",
                                                      text_color=TEXT_DIM, font=(FONT, 12))
            self.dash_activity_empty.grid(row=0, column=0, sticky="w", pady=6)
            return
        for i, entry in enumerate(recent):
            self._make_activity_row(self.dash_activity_list, entry, i)

    def _make_activity_row(self, parent, entry, row):
        status_color = {"finished": GREEN, "stopped": TEXT_DIM, "error": RED}.get(entry["status"], TEXT_DIM)
        r = ctk.CTkFrame(parent, fg_color=CARD, corner_radius=8, border_width=1, border_color=BORDER)
        r.grid(row=row, column=0, sticky="ew", pady=4)
        r.columnconfigure(1, weight=1)
        ctk.CTkLabel(r, text="●", text_color=status_color, font=(FONT, 12), width=16).grid(
            row=0, column=0, padx=(12, 4), pady=8)
        ctk.CTkLabel(r, text=entry["name"], text_color=TEXT, font=(FONT, 12), anchor="w").grid(
            row=0, column=1, sticky="w", pady=8)
        ctk.CTkLabel(r, text=fmt_duration(entry["duration"]), text_color=TEXT_DIM, font=(FONT, 11)).grid(
            row=0, column=2, padx=10, pady=8)
        ctk.CTkLabel(r, text=fmt_when(entry["start"]), text_color=TEXT_DIM, font=(FONT, 11)).grid(
            row=0, column=3, padx=(0, 12), pady=8)

    # ============================================================= MACROS
    def _build_macros_page(self, page):
        page.columnconfigure(0, weight=0, minsize=280)
        page.columnconfigure(1, weight=1)
        page.rowconfigure(0, weight=1)

        left = Card(page)
        left.grid(row=0, column=0, sticky="nsew", padx=(0, 16))
        left.columnconfigure(0, weight=1)
        left.rowconfigure(1, weight=1)

        list_header = ctk.CTkFrame(left, fg_color="transparent")
        list_header.grid(row=0, column=0, sticky="ew", padx=16, pady=(16, 10))
        list_header.columnconfigure(0, weight=1)
        ctk.CTkLabel(list_header, text="MACROS", text_color=TEXT_DIM, font=(FONT, 11, "bold")).grid(
            row=0, column=0, sticky="w")
        ctk.CTkButton(
            list_header, text="⟳", width=28, height=28, corner_radius=8,
            fg_color=CARD, hover_color=CARD_HOVER, text_color=TEXT, border_width=1, border_color=BORDER,
            command=self.refresh_macros,
        ).grid(row=0, column=1, sticky="e")

        self.list_scroll = ctk.CTkScrollableFrame(left, fg_color="transparent")
        self.list_scroll.grid(row=1, column=0, sticky="nsew", padx=10)
        self.list_scroll.columnconfigure(0, weight=1)

        list_footer = ctk.CTkFrame(left, fg_color="transparent")
        list_footer.grid(row=2, column=0, sticky="ew", padx=16, pady=14)
        ctk.CTkButton(
            list_footer, text="＋ New Macro", corner_radius=10, height=34,
            fg_color=CARD, hover_color=CARD_HOVER, text_color=TEXT, border_width=1, border_color=BORDER,
            command=self.new_macro,
        ).pack(fill="x", pady=(0, 8))
        self.copy_prompt_btn = ctk.CTkButton(
            list_footer, text="📋 Copy AI Prompt", corner_radius=10, height=34,
            fg_color=CARD, hover_color=CARD_HOVER, text_color=TEXT, border_width=1, border_color=BORDER,
            command=self._copy_ai_prompt,
        )
        self.copy_prompt_btn.pack(fill="x", pady=(0, 8))
        ctk.CTkButton(
            list_footer, text="📁 Open Folder", corner_radius=10, height=34,
            fg_color=CARD, hover_color=CARD_HOVER, text_color=TEXT, border_width=1, border_color=BORDER,
            command=self.open_macros_folder,
        ).pack(fill="x")

        right = Card(page)
        right.grid(row=0, column=1, sticky="nsew")
        right.columnconfigure(0, weight=1)
        right.rowconfigure(3, weight=1)

        info_row = ctk.CTkFrame(right, fg_color="transparent")
        info_row.grid(row=0, column=0, sticky="ew", padx=20, pady=(20, 4))
        info_row.columnconfigure(0, weight=1)
        self.selected_label = ctk.CTkLabel(info_row, text="No macro selected", text_color=TEXT,
                                            font=(FONT, 17, "bold"), anchor="w")
        self.selected_label.grid(row=0, column=0, sticky="w")
        self.status_badge = ctk.CTkLabel(
            info_row, text="● STOPPED", text_color=RED, fg_color=CARD, corner_radius=8,
            font=(FONT, 12, "bold"), height=28, padx=12, border_width=1, border_color=BORDER,
        )
        self.status_badge.grid(row=0, column=1, sticky="e")

        controls = ctk.CTkFrame(right, fg_color="transparent")
        controls.grid(row=1, column=0, sticky="ew", padx=20, pady=(14, 6))
        self.start_btn = ctk.CTkButton(
            controls, text="▶  Start", corner_radius=10, height=38, width=120,
            fg_color=ACCENT, hover_color=ACCENT_HOVER, text_color="#00120d",
            text_color_disabled="#4f8f7c", font=(FONT, 13, "bold"), command=self.start_macro,
        )
        self.start_btn.pack(side="left")
        self.stop_btn = ctk.CTkButton(
            controls, text="■  Stop", corner_radius=10, height=38, width=110,
            fg_color=RED, hover_color=RED_HOVER, text_color="#2b000a",
            text_color_disabled="#a34a5c", font=(FONT, 13, "bold"), command=self.stop_macro, state="disabled",
        )
        self.stop_btn.pack(side="left", padx=10)
        self.force_btn = ctk.CTkButton(
            controls, text="Force Stop", corner_radius=10, height=38, width=110,
            fg_color=CARD, hover_color=CARD_HOVER, text_color=TEXT, text_color_disabled=TEXT_DIM,
            border_width=1, border_color=BORDER, font=(FONT, 12), command=self.force_stop_macro, state="disabled",
        )
        self.force_btn.pack(side="left")

        def macros_status_cb(status):
            if status == "running":
                self.status_badge.configure(text="● RUNNING", text_color=GREEN)
                self.start_btn.configure(state="disabled", fg_color=ACCENT_DIM)
                self.stop_btn.configure(state="normal", fg_color=RED)
                self.force_btn.configure(state="normal")
            else:
                self.status_badge.configure(text="● STOPPED", text_color=RED)
                self.start_btn.configure(state="normal", fg_color=ACCENT)
                self.stop_btn.configure(state="disabled", fg_color=RED_DIM)
                self.force_btn.configure(state="disabled")
        self._status_listeners.append(macros_status_cb)

        def macros_select_cb(name):
            self.selected_label.configure(text=name or "No macro selected")
        self._selection_listeners.append(macros_select_cb)

        log_section = ctk.CTkFrame(right, fg_color="transparent")
        log_section.grid(row=3, column=0, sticky="nsew", padx=20, pady=(10, 20))
        log_section.columnconfigure(0, weight=1)
        log_section.rowconfigure(1, weight=1)
        log_top = ctk.CTkFrame(log_section, fg_color="transparent")
        log_top.grid(row=0, column=0, sticky="ew", pady=(0, 8))
        log_top.columnconfigure(0, weight=1)
        ctk.CTkLabel(log_top, text="LOG", text_color=TEXT_DIM, font=(FONT, 11, "bold")).grid(row=0, column=0, sticky="w")
        ctk.CTkButton(
            log_top, text="Clear", width=60, height=26, corner_radius=8,
            fg_color=CARD, hover_color=CARD_HOVER, text_color=TEXT_DIM, font=(FONT, 11),
            border_width=1, border_color=BORDER, command=self._clear_log,
        ).grid(row=0, column=1, sticky="e")

        self.log_text = ctk.CTkTextbox(
            log_section, fg_color="#020302", text_color=GREEN, font=(FONT_MONO, 12),
            corner_radius=12, wrap="word", border_width=1, border_color=BORDER_STRONG,
        )
        self.log_text.grid(row=1, column=0, sticky="nsew")
        self.log_text.configure(state="disabled")

    # ============================================================ HISTORY
    def _build_history_page(self, page):
        page.columnconfigure(0, weight=1)
        page.rowconfigure(0, weight=1)

        card = Card(page)
        card.grid(row=0, column=0, sticky="nsew")
        card.columnconfigure(0, weight=1)
        card.rowconfigure(1, weight=1)

        top = ctk.CTkFrame(card, fg_color="transparent")
        top.grid(row=0, column=0, sticky="ew", padx=20, pady=(18, 8))
        top.columnconfigure(0, weight=1)
        ctk.CTkLabel(top, text="RUN HISTORY", text_color=TEXT_DIM, font=(FONT, 11, "bold")).grid(
            row=0, column=0, sticky="w")
        ctk.CTkButton(
            top, text="Clear History", width=110, height=28, corner_radius=8,
            fg_color=CARD, hover_color=CARD_HOVER, text_color=TEXT_DIM, font=(FONT, 11),
            border_width=1, border_color=BORDER, command=self._clear_history,
        ).grid(row=0, column=1, sticky="e")

        self.history_scroll = ctk.CTkScrollableFrame(card, fg_color="transparent")
        self.history_scroll.grid(row=1, column=0, sticky="nsew", padx=14, pady=(0, 18))
        self.history_scroll.columnconfigure(0, weight=1)

    def _refresh_history_list(self):
        for w in self.history_scroll.winfo_children():
            w.destroy()
        if not self.history:
            ctk.CTkLabel(self.history_scroll, text="No runs recorded yet.", text_color=TEXT_DIM,
                         font=(FONT, 12)).grid(row=0, column=0, sticky="w", pady=8)
            return
        for i, entry in enumerate(reversed(self.history)):
            self._make_activity_row(self.history_scroll, entry, i)

    def _clear_history(self):
        if not self.history:
            return
        if not messagebox.askyesno("AutoFlow", "Clear all run history? This can't be undone."):
            return
        self.history = []
        save_history(self.history)
        self._refresh_history_list()
        self._refresh_dashboard()

    # =========================================================== SETTINGS
    def _build_settings_page(self, page):
        page.columnconfigure(0, weight=1)

        card = Card(page)
        card.grid(row=0, column=0, sticky="ew")
        card.columnconfigure(0, weight=1)
        inner = ctk.CTkFrame(card, fg_color="transparent")
        inner.grid(row=0, column=0, sticky="ew", padx=24, pady=22)
        inner.columnconfigure(0, weight=1)

        ctk.CTkLabel(inner, text="HOTKEY", text_color=TEXT_DIM, font=(FONT, 11, "bold")).grid(
            row=0, column=0, sticky="w")
        hk_row = ctk.CTkFrame(inner, fg_color="transparent")
        hk_row.grid(row=1, column=0, sticky="w", pady=(8, 4))
        self.hotkey_capture_btn = ctk.CTkButton(
            hk_row, text=self.cfg.get("hotkey", "f8").upper(), width=160, height=36, corner_radius=8,
            fg_color=CARD, hover_color=CARD_HOVER, border_width=1, border_color=BORDER,
            text_color=TEXT, font=(FONT, 13, "bold"), command=self._start_hotkey_capture,
        )
        self.hotkey_capture_btn.pack(side="left")
        ctk.CTkLabel(hk_row, text="click, then press the key you want", text_color=TEXT_DIM,
                     font=(FONT, 11)).pack(side="left", padx=10)
        self.hotkey_status_label = ctk.CTkLabel(
            inner, text="", text_color=TEXT_DIM, font=(FONT, 11),
        )
        self.hotkey_status_label.grid(row=2, column=0, sticky="w")
        self._update_hotkey_status_label()

        divider1 = ctk.CTkFrame(inner, fg_color=BORDER, height=1)
        divider1.grid(row=3, column=0, sticky="ew", pady=18)

        ctk.CTkLabel(inner, text="MACROS FOLDER", text_color=TEXT_DIM, font=(FONT, 11, "bold")).grid(
            row=4, column=0, sticky="w")
        ctk.CTkLabel(inner, text=MACROS_DIR, text_color=TEXT, font=(FONT, 12)).grid(
            row=5, column=0, sticky="w", pady=(8, 10))
        ctk.CTkButton(
            inner, text="📁 Open Folder", corner_radius=10, height=34, width=150,
            fg_color=CARD, hover_color=CARD_HOVER, text_color=TEXT, border_width=1, border_color=BORDER,
            command=self.open_macros_folder,
        ).grid(row=6, column=0, sticky="w")

        divider2 = ctk.CTkFrame(inner, fg_color=BORDER, height=1)
        divider2.grid(row=7, column=0, sticky="ew", pady=18)

        ctk.CTkLabel(inner, text="SYSTEM TRAY", text_color=TEXT_DIM, font=(FONT, 11, "bold")).grid(
            row=8, column=0, sticky="w")
        if TRAY_AVAILABLE:
            ctk.CTkLabel(
                inner,
                text="Closing this window minimizes AutoFlow to the system tray instead of\n"
                     "quitting — the hotkey and any running macro keep working. Right-click\n"
                     "the tray icon to reopen the window or quit for real.",
                text_color=TEXT_DIM, font=(FONT, 12), justify="left",
            ).grid(row=9, column=0, sticky="w", pady=(8, 10))
        else:
            ctk.CTkLabel(
                inner,
                text="Not available — install 'pystray' to enable minimize-to-tray.\n"
                     "Run: pip install pystray",
                text_color=TEXT_DIM, font=(FONT, 12), justify="left",
            ).grid(row=9, column=0, sticky="w", pady=(8, 10))
        ctk.CTkButton(
            inner, text="Quit AutoFlow", corner_radius=10, height=34, width=150,
            fg_color=RED_DIM, hover_color=RED, text_color=TEXT, border_width=1, border_color=BORDER,
            command=self._quit_app,
        ).grid(row=10, column=0, sticky="w")

        divider3 = ctk.CTkFrame(inner, fg_color=BORDER, height=1)
        divider3.grid(row=11, column=0, sticky="ew", pady=18)

        ctk.CTkLabel(inner, text="ABOUT", text_color=TEXT_DIM, font=(FONT, 11, "bold")).grid(
            row=12, column=0, sticky="w")
        ctk.CTkLabel(
            inner,
            text="AutoFlow runs Python macros you drop into the macros/ folder.\n"
                 "Each macro defines run(stop_event) and should check it periodically\n"
                 "so it can stop cleanly.",
            text_color=TEXT_DIM, font=(FONT, 12), justify="left",
        ).grid(row=13, column=0, sticky="w", pady=(8, 0))

    def _update_hotkey_status_label(self):
        if self.hotkey_lib_ok:
            self.hotkey_status_label.configure(text="Registered globally — works even when AutoFlow isn't focused.")
        else:
            self.hotkey_status_label.configure(
                text="Local only (install the 'keyboard' package for a global hotkey) — works while AutoFlow is focused.")

    def _start_hotkey_capture(self):
        try:
            import keyboard  # noqa: F401 — just checking it's importable
        except ImportError:
            messagebox.showerror("AutoFlow", "This needs the 'keyboard' package.\nRun: pip install keyboard")
            return

        self.hotkey_capture_btn.configure(text="Press a key…", state="disabled")

        def worker():
            try:
                import keyboard
                new_hk = keyboard.read_hotkey(suppress=False)
                self.after(0, lambda: self._finish_hotkey_capture(new_hk))
            except Exception as e:
                self.after(0, lambda: self._finish_hotkey_capture(None, error=str(e)))

        threading.Thread(target=worker, daemon=True).start()

    def _finish_hotkey_capture(self, new_hk, error=None):
        self.hotkey_capture_btn.configure(state="normal")
        if error or not new_hk:
            self.hotkey_capture_btn.configure(text=self.cfg.get("hotkey", "f8").upper())
            if error:
                messagebox.showerror("AutoFlow", f"Couldn't capture a key: {error}")
            return

        self.cfg["hotkey"] = new_hk
        save_config(self.cfg)
        self.hotkey_capture_btn.configure(text=new_hk.upper())
        self._apply_hotkey(new_hk)

    # -------------------------------------------------------------- macros
    def refresh_macros(self):
        os.makedirs(MACROS_DIR, exist_ok=True)
        self.macros = {}
        for fname in sorted(os.listdir(MACROS_DIR)):
            if fname.endswith(".py") and not fname.startswith("_"):
                name = fname[:-3]
                self.macros[name] = os.path.join(MACROS_DIR, fname)

        for row in self.rows.values():
            row.destroy()
        self.rows = {}

        for i, name in enumerate(self.macros):
            row = MacroRow(self.list_scroll, name, self._on_select)
            row.grid(row=i, column=0, sticky="ew", pady=4)
            self.rows[name] = row

        last = self.cfg.get("last_selected")
        if last and last in self.macros:
            self._on_select(last)
        elif self.macros and not self.selected_name:
            self._on_select(next(iter(self.macros)))
        elif self.selected_name not in self.macros:
            self.selected_name = None
            for cb in self._selection_listeners:
                cb(None)

        if hasattr(self, "stat_macros"):
            self._refresh_dashboard()

    def _on_select(self, name):
        if self.selected_name and self.selected_name in self.rows:
            self.rows[self.selected_name].set_selected(False)
        self.selected_name = name
        if name in self.rows:
            self.rows[name].set_selected(True)
        for cb in self._selection_listeners:
            cb(name)
        self.cfg["last_selected"] = name
        save_config(self.cfg)

    def _copy_ai_prompt(self):
        try:
            self.clipboard_clear()
            self.clipboard_append(AI_MACRO_PROMPT)
            self.update()  # keeps the clipboard content available after the app loses focus
        except Exception:
            messagebox.showerror("AutoFlow", "Couldn't access the clipboard.")
            return

        original_text = "📋 Copy AI Prompt"
        self.copy_prompt_btn.configure(text="✓ Copied!", text_color=ACCENT, border_color=ACCENT)
        self.after(1500, lambda: self.copy_prompt_btn.configure(
            text=original_text, text_color=TEXT, border_color=BORDER))

    def new_macro(self):
        name = simpledialog.askstring("New Macro", "Macro name (no .py needed):", parent=self)
        if not name:
            return
        name = name.strip().replace(" ", "_")
        path = os.path.join(MACROS_DIR, f"{name}.py")
        if os.path.exists(path):
            messagebox.showerror("AutoFlow", "A macro with that name already exists.")
            return
        template = (
            '"""\n'
            "HOW TO WRITE THIS MACRO\n"
            "-------------------------\n"
            "Replace the run() function below with your own logic. Every macro\n"
            "must define:\n\n"
            "    def run(stop_event):\n"
            "        ...\n\n"
            "`stop_event` lets AutoFlow stop your macro cleanly (Stop button, or\n"
            "pressing the hotkey again). Use the sleep() helper instead of\n"
            "time.sleep() so Stop actually works:\n\n"
            "    if sleep(1, stop_event):   # waits 1s, returns True if stopped early\n"
            "        return\n\n"
            "Anything you print() shows up in AutoFlow's LOG panel.\n\n"
            "New to this? Click \"Copy AI Prompt\" next to New Macro, paste it into\n"
            "an AI chatbot with a description of what you want, and paste what it\n"
            "gives back in here.\n"
            '"""\n'
            "from macro_api import sleep\n\n\n"
            "def run(stop_event):\n"
            "    pass  # <- replace this with your macro's logic\n"
        )
        with open(path, "w", encoding="utf-8") as f:
            f.write(template)
        self.refresh_macros()
        self._on_select(name)
        self._open_in_editor(path)

    def _open_in_editor(self, path):
        try:
            if sys.platform.startswith("win"):
                os.startfile(path)  # noqa
            elif sys.platform == "darwin":
                subprocess.Popen(["open", path])
            else:
                subprocess.Popen(["xdg-open", path])
        except Exception:
            pass

    def open_macros_folder(self):
        try:
            if sys.platform.startswith("win"):
                os.startfile(MACROS_DIR)  # noqa
            elif sys.platform == "darwin":
                subprocess.Popen(["open", MACROS_DIR])
            else:
                subprocess.Popen(["xdg-open", MACROS_DIR])
        except Exception as e:
            messagebox.showerror("AutoFlow", f"Couldn't open folder: {e}")

    # ------------------------------------------------------------- running
    def toggle_run(self):
        if self.status == "running":
            self.stop_macro()
        else:
            self.start_macro()

    def start_macro(self):
        if self.status == "running":
            return
        name = self.selected_name
        if not name or name not in self.macros:
            self._log("[!] No macro selected.\n")
            return

        path = self.macros[name]
        try:
            spec = importlib.util.spec_from_file_location(name, path)
            module = importlib.util.module_from_spec(spec)
            sys.path.insert(0, BASE_DIR)
            spec.loader.exec_module(module)
        except Exception as e:
            self._log(f"[!] Failed to load '{name}': {e}\n")
            return

        if not hasattr(module, "run"):
            self._log(f"[!] Macro '{name}' has no run(stop_event) function.\n")
            return

        self.stop_event = threading.Event()
        self._last_run_error = None
        self.current_run = {"name": name, "start": time.time()}
        self._log(f"\n=== Starting '{name}' — {time.strftime('%H:%M:%S')} ===\n")

        def target():
            sys.stdout = QueueWriter(self.log_queue)
            try:
                module.run(self.stop_event)
            except Exception as e:
                self._last_run_error = str(e)
                self.log_queue.put(f"[!] Macro error: {e}\n")
            finally:
                sys.stdout = self._orig_stdout

        self.run_thread = threading.Thread(target=target, daemon=True)
        self.run_thread.start()
        self._set_status("running")

    def stop_macro(self):
        if self.status != "running":
            return
        self.stop_event.set()
        self._log("[i] Stop requested — waiting for macro to notice...\n")

    def force_stop_macro(self):
        if not self.run_thread or not self.run_thread.is_alive():
            return
        if not messagebox.askyesno("Force Stop", "Force-killing a thread is unsafe and can leave things in a "
                                                  "weird state (e.g. mid keypress). Continue?"):
            return
        tid = self.run_thread.ident
        if tid is not None:
            ctypes.pythonapi.PyThreadState_SetAsyncExc(ctypes.c_long(tid), ctypes.py_object(SystemExit))
        self._log("[!] Force stop sent.\n")
        sys.stdout = self._orig_stdout

    def _poll_thread_status(self):
        if self.status == "running" and self.run_thread is not None and not self.run_thread.is_alive():
            self._log(f"=== Finished — {time.strftime('%H:%M:%S')} ===\n")
            if self.current_run:
                end = time.time()
                if self._last_run_error:
                    status = "error"
                elif self.stop_event.is_set():
                    status = "stopped"
                else:
                    status = "finished"
                self.history.append({
                    "name": self.current_run["name"],
                    "start": self.current_run["start"],
                    "duration": end - self.current_run["start"],
                    "status": status,
                })
                self.history = self.history[-HISTORY_LIMIT:]
                save_history(self.history)
                self.current_run = None
            self._set_status("stopped")
            self._refresh_dashboard()
        self.after(150, self._poll_thread_status)

    def _set_status(self, status):
        self.status = status
        for cb in self._status_listeners:
            cb(status)
        if self.selected_name in self.rows:
            self.rows[self.selected_name].set_status(status)

    # ------------------------------------------------------------- logging
    def _log(self, text):
        self.log_queue.put(text)

    def _poll_log_queue(self):
        drained = False
        while True:
            try:
                text = self.log_queue.get_nowait()
            except queue.Empty:
                break
            self.log_text.configure(state="normal")
            self.log_text.insert("end", text)
            self.log_text.configure(state="disabled")
            drained = True
        if drained:
            self.log_text.see("end")
        self.after(100, self._poll_log_queue)

    def _clear_log(self):
        self.log_text.configure(state="normal")
        self.log_text.delete("1.0", "end")
        self.log_text.configure(state="disabled")

    # -------------------------------------------------------------- hotkey
    def _register_hotkey(self):
        self._apply_hotkey(self.cfg.get("hotkey", "f8"))

    def _local_tk_sequence(self, hk):
        """Translate a simple keyboard-lib hotkey string into a Tk bind
        sequence for the focused-window fallback. Only single keys and F-keys
        are supported here — combos (ctrl+alt+m) rely on the global hook only,
        since Tk's own multi-modifier bindings are unreliable cross-platform."""
        hk = hk.strip().lower()
        if hk.startswith("f") and hk[1:].isdigit() and 1 <= int(hk[1:]) <= 24:
            return f"<F{hk[1:]}>"
        if len(hk) == 1:
            return f"<Key-{hk}>"
        return None  # combo — no local fallback binding

    def _apply_hotkey(self, new_hk, on_done=None):
        """The single path for setting the active hotkey. Always tears down
        whatever was previously active (both the global keyboard hook and the
        local Tk-focused fallback) before registering the new one, so we never
        end up with two different hotkeys both working at once."""
        new_hk = new_hk.strip().lower()
        old_hk = self._active_hotkey
        old_seq = self._local_bind_seq

        self.hotkey_badge.configure(text=new_hk.upper())
        self.hotkey_desc.configure(text="registering…")

        # Local Tk fallback: unbind the old sequence, bind the new one (if it's
        # a simple key we can translate). This is what makes the hotkey work
        # while AutoFlow itself is focused, independent of the global hook.
        if old_seq:
            self.unbind_all(old_seq)
        new_seq = self._local_tk_sequence(new_hk)
        if new_seq:
            self.bind_all(new_seq, lambda e: self.toggle_run())
        self._local_bind_seq = new_seq

        def worker():
            try:
                import keyboard
                if old_hk:
                    try:
                        keyboard.remove_hotkey(old_hk)
                    except Exception:
                        pass  # wasn't registered, or already gone — fine either way
                keyboard.add_hotkey(new_hk, lambda: self.after(0, self.toggle_run))
                self._active_hotkey = new_hk
                self.after(0, lambda: self._on_hotkey_registered(new_hk, True, on_done))
            except Exception:
                self._active_hotkey = None
                self.after(0, lambda: self._on_hotkey_registered(new_hk, False, on_done))

        threading.Thread(target=worker, daemon=True).start()

    def _on_hotkey_registered(self, hk, ok, on_done=None):
        self.hotkey_lib_ok = ok
        self.hotkey_badge.configure(text=hk.upper())
        self.hotkey_desc.configure(text="toggle start/stop · global" if ok
                                    else "toggle start/stop · app-focused only")
        if hasattr(self, "hotkey_status_label"):
            self._update_hotkey_status_label()
        if on_done:
            on_done(ok)

    def _on_close(self):
        if self.tray_icon is not None:
            self.withdraw()  # hide to tray, keep running (hotkey/macros stay live)
            return
        if self.status == "running":
            if not messagebox.askyesno("AutoFlow", "A macro is still running. Quit anyway?"):
                return
            self.stop_event.set()
        save_config(self.cfg)
        sock = getattr(self, "_single_instance_sock", None)
        if sock is not None:
            try:
                sock.close()
            except Exception:
                pass
        self.destroy()


if __name__ == "__main__":
    os.makedirs(MACROS_DIR, exist_ok=True)
    lock_socket = _try_acquire_single_instance_lock()
    if lock_socket is None:
        # Another instance is already running — it's been pinged to raise its
        # window, so just exit quietly instead of opening a duplicate.
        sys.exit(0)
    app = AutoFlowApp()
    app._start_single_instance_listener(lock_socket)
    app.mainloop()
