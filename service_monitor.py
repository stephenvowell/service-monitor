"""Service Health Monitor.

A tiny, dependency-free desktop app that pings your services and shows a
green/red status at a glance. Styled to match the MedAlert Health Board:
dark navy background, blue accent, rounded cards, colored status pills.

Same spirit as the backup app: one file, plain Tkinter, no external packages.

Check types:
  - http    : GET the URL; UP if status <= expect_max, WARN if <500, else DOWN
  - tcp     : open a socket to host:port; UP if it connects
  - ping    : system ping; UP if the host replies (shows round-trip ms)
  - netperf : measures this PC's upload speed (Cloudflare) + ping latency

Config lives in `service_monitor_config.json` next to this file (or next to
the .exe when frozen with PyInstaller). Edit it in the app or by hand.
"""

import json
import os
import re
import socket
import subprocess
import sys
import threading
import time
import tkinter as tk
import urllib.error
import urllib.request
from tkinter import messagebox, simpledialog

try:
    import winsound  # Windows-only; used for the failure beep.
except ImportError:  # pragma: no cover - non-Windows fallback
    winsound = None

APP_NAME = "Service Health Monitor"
__version__ = "1.0.0"
HTTP_TIMEOUT = 8
TCP_TIMEOUT = 5

# Cloudflare's public speed-test endpoints (no key needed).
SPEED_UP_URL = "https://speed.cloudflare.com/__up"
SPEED_DOWN_URL = "https://speed.cloudflare.com/__down?bytes="
UPLOAD_BYTES = 2 * 1024 * 1024      # 2 MB per upload test
DOWNLOAD_BYTES = 10 * 1024 * 1024   # 10 MB per download test
NETPERF_TIMEOUT = 30

# --- Palette (from the MedAlert board's styles.css) -------------------------
BG = "#0b1020"
PANEL = "#121933"
CARD_BG = "#151d3a"
CARD_BORDER = "#2a3358"
TEXT = "#edf2ff"
MUTED = "#9aa7c7"
ACCENT = "#7c9cff"
ACCENT_STRONG = "#5b7cfa"
SUCCESS = "#5ee1a2"
WARN = "#ffe566"
DANGER = "#ff7b7b"

# Status pill styling: (text color, fill, border)
PILL = {
    "UP": (SUCCESS, "#123027", "#2f6b52"),
    "WARN": (WARN, "#332f10", "#6f6420"),
    "DOWN": ("#ff9b9b", "#331620", "#7a2b39"),
    "CHECKING": (MUTED, "#1a2242", CARD_BORDER),
}
VALUE_COLOR = {"UP": TEXT, "WARN": WARN, "DOWN": DANGER, "CHECKING": MUTED}

FONT = "Segoe UI"
CARD_H = 122
CARD_MIN_W = 250
GAP = 14

INTERVAL_CHOICES = {"15 sec": 15, "30 sec": 30, "1 min": 60, "5 min": 300}

DEFAULT_CONFIG = {
    "settings": {
        "interval_label": "30 sec",
        "auto_refresh": True,
        "sound_on_failure": True,
    },
    "services": [
        {"name": "Website (stephenv.net)", "type": "http",
         "target": "https://stephenv.net/", "expect_max": 399},
        {"name": "Health Board", "type": "http",
         "target": "https://medalert.stephenv.net/", "expect_max": 399},
        {"name": "Cam (public)", "type": "http",
         "target": "https://cam.stephenv.net/api/frame.jpeg?src=espcam",
         "expect_max": 399},
        {"name": "Raspberry Pi", "type": "tcp",
         "target": "192.168.12.158", "port": 22},
        {"name": "ESP32 Cam (LAN)", "type": "http",
         "target": "http://192.168.12.220/", "expect_max": 405},
        {"name": "Internet Download", "type": "netdown",
         "target": "1.1.1.1", "min_interval": 300},
        {"name": "Internet Uplink", "type": "netperf",
         "target": "1.1.1.1", "min_interval": 300},
    ],
}


def app_dir():
    """Folder for the config file: next to the .exe when frozen, else the script."""
    if getattr(sys, "frozen", False):
        return os.path.dirname(sys.executable)
    return os.path.dirname(os.path.abspath(__file__))


CONFIG_PATH = os.path.join(app_dir(), "service_monitor_config.json")


def load_config():
    if os.path.exists(CONFIG_PATH):
        try:
            with open(CONFIG_PATH, "r", encoding="utf-8") as fh:
                data = json.load(fh)
            data.setdefault("settings", dict(DEFAULT_CONFIG["settings"]))
            data.setdefault("services", [])
            for key, val in DEFAULT_CONFIG["settings"].items():
                data["settings"].setdefault(key, val)
            return data
        except (OSError, ValueError):
            pass
    save_config(DEFAULT_CONFIG)
    return json.loads(json.dumps(DEFAULT_CONFIG))


def save_config(config):
    try:
        with open(CONFIG_PATH, "w", encoding="utf-8") as fh:
            json.dump(config, fh, indent=2)
    except OSError as exc:
        print(f"Could not save config: {exc}", file=sys.stderr)


# --- Checks -----------------------------------------------------------------
# Each checker returns a dict: {"state", "detail", "value"}.

def classify_http(code, expect_max):
    """Map an HTTP status code to a health state (pure, no I/O)."""
    if code <= expect_max:
        return "UP"
    if code < 500:
        return "WARN"
    return "DOWN"


def mbps(num_bytes, seconds):
    """Throughput in megabits per second, or None if not measurable."""
    if not seconds or seconds <= 0 or not num_bytes:
        return None
    return (num_bytes * 8) / seconds / 1e6


def check_http(svc):
    url = svc["target"]
    expect_max = int(svc.get("expect_max", 399))
    req = urllib.request.Request(
        url, method="GET", headers={"User-Agent": "ServiceHealthMonitor/1.0"}
    )
    start = time.perf_counter()
    try:
        with urllib.request.urlopen(req, timeout=HTTP_TIMEOUT) as resp:
            code = resp.getcode()
    except urllib.error.HTTPError as exc:
        code = exc.code  # Server responded, just with an error status.
    except (urllib.error.URLError, socket.timeout, ConnectionError) as exc:
        reason = getattr(exc, "reason", exc)
        return {"state": "DOWN", "detail": f"{reason}", "value": "\u2014"}
    except Exception as exc:  # noqa: BLE001 - never let a check crash the loop
        return {"state": "DOWN", "detail": f"{exc}", "value": "\u2014"}

    latency = int((time.perf_counter() - start) * 1000)
    state = classify_http(code, expect_max)
    value = "\u2014" if state == "DOWN" else f"{latency} ms"
    return {"state": state, "detail": f"HTTP {code}", "value": value}


def check_tcp(svc):
    host = svc["target"]
    port = int(svc.get("port", 80))
    start = time.perf_counter()
    try:
        with socket.create_connection((host, port), timeout=TCP_TIMEOUT):
            latency = int((time.perf_counter() - start) * 1000)
            return {"state": "UP", "detail": f"port {port} open",
                    "value": f"{latency} ms"}
    except (OSError, socket.timeout) as exc:
        return {"state": "DOWN", "detail": f"port {port}: {exc}", "value": "\u2014"}


def measure_ping_rtt(host):
    """Return average round-trip ms parsed from the system ping, or None."""
    param = "-n" if os.name == "nt" else "-c"
    try:
        out = subprocess.run(
            ["ping", param, "3", host], capture_output=True, text=True,
            timeout=12, creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        ).stdout
    except (subprocess.SubprocessError, OSError):
        return None
    m = re.search(r"Average = (\d+)ms", out)           # Windows
    if m:
        return int(m.group(1))
    m = re.search(r"=\s*[\d.]+/([\d.]+)/", out)          # Linux/mac rtt avg
    if m:
        return int(float(m.group(1)))
    m = re.search(r"time[=<]\s*([\d.]+)\s*ms", out)      # single-reply fallback
    if m:
        return int(float(m.group(1)))
    return None


def check_ping(svc):
    rtt = measure_ping_rtt(svc["target"])
    if rtt is None:
        return {"state": "DOWN", "detail": "no reply", "value": "\u2014"}
    return {"state": "UP", "detail": "reply", "value": f"{rtt} ms"}


def measure_upload():
    """POST a fixed chunk to Cloudflare and return Mbps, or None on failure."""
    data = b"\0" * UPLOAD_BYTES
    req = urllib.request.Request(
        SPEED_UP_URL, data=data, method="POST",
        headers={"Content-Type": "application/octet-stream",
                 "User-Agent": "ServiceHealthMonitor/1.0"},
    )
    start = time.perf_counter()
    with urllib.request.urlopen(req, timeout=NETPERF_TIMEOUT) as resp:
        resp.read()
    return mbps(UPLOAD_BYTES, time.perf_counter() - start)


def measure_download():
    """GET a fixed chunk from Cloudflare and return Mbps, or None on failure."""
    req = urllib.request.Request(
        SPEED_DOWN_URL + str(DOWNLOAD_BYTES),
        headers={"User-Agent": "ServiceHealthMonitor/1.0"},
    )
    start = time.perf_counter()
    total = 0
    with urllib.request.urlopen(req, timeout=NETPERF_TIMEOUT) as resp:
        while True:
            chunk = resp.read(65536)
            if not chunk:
                break
            total += len(chunk)
    return mbps(total, time.perf_counter() - start)


def check_netperf(svc):
    host = svc.get("target", "1.1.1.1")
    ping_ms = measure_ping_rtt(host)
    try:
        up = measure_upload()
    except (urllib.error.URLError, socket.timeout, ConnectionError, OSError) as exc:
        reason = getattr(exc, "reason", exc)
        return {"state": "DOWN", "detail": f"upload failed: {reason}",
                "value": "\u2014"}
    except Exception as exc:  # noqa: BLE001
        return {"state": "DOWN", "detail": f"upload failed: {exc}", "value": "\u2014"}
    if up is None:
        return {"state": "DOWN", "detail": "upload failed", "value": "\u2014"}
    detail = f"ping {ping_ms} ms" if ping_ms is not None else "ping n/a"
    return {"state": "UP", "detail": detail, "value": f"\u2191 {up:.1f} Mbps"}


def check_netdown(svc):
    host = svc.get("target", "1.1.1.1")
    ping_ms = measure_ping_rtt(host)
    try:
        down = measure_download()
    except (urllib.error.URLError, socket.timeout, ConnectionError, OSError) as exc:
        reason = getattr(exc, "reason", exc)
        return {"state": "DOWN", "detail": f"download failed: {reason}",
                "value": "\u2014"}
    except Exception as exc:  # noqa: BLE001
        return {"state": "DOWN", "detail": f"download failed: {exc}", "value": "\u2014"}
    if down is None:
        return {"state": "DOWN", "detail": "download failed", "value": "\u2014"}
    detail = f"ping {ping_ms} ms" if ping_ms is not None else "ping n/a"
    return {"state": "UP", "detail": detail, "value": f"\u2193 {down:.1f} Mbps"}


CHECKERS = {"http": check_http, "tcp": check_tcp, "ping": check_ping,
            "netperf": check_netperf, "netdown": check_netdown}


def run_check(svc):
    checker = CHECKERS.get(svc.get("type", "http"), check_http)
    try:
        return checker(svc)
    except Exception as exc:  # noqa: BLE001
        return {"state": "DOWN", "detail": f"{exc}", "value": "\u2014"}


# --- Canvas helpers ---------------------------------------------------------

def round_rect(canvas, x1, y1, x2, y2, r, **kwargs):
    """Draw a smooth rounded rectangle as a polygon."""
    r = min(r, (x2 - x1) / 2, (y2 - y1) / 2)
    pts = [
        x1 + r, y1, x2 - r, y1, x2, y1, x2, y1 + r,
        x2, y2 - r, x2, y2, x2 - r, y2, x1 + r, y2,
        x1, y2, x1, y2 - r, x1, y1 + r, x1, y1,
    ]
    return canvas.create_polygon(pts, smooth=True, **kwargs)


def target_label(svc):
    target = svc.get("target", "")
    if svc.get("type") == "tcp":
        return f"{target}:{svc.get('port', '')}"
    if svc.get("type") == "netperf":
        return f"upload test \u2022 ping {target}"
    if svc.get("type") == "netdown":
        return f"download test \u2022 ping {target}"
    return target


def elide(text, limit):
    return text if len(text) <= limit else text[: limit - 1] + "\u2026"


def new_result(state="CHECKING", detail="checking", value="\u2026", checked=""):
    return {"state": state, "detail": detail, "value": value, "checked": checked}


# --- UI ---------------------------------------------------------------------

class MonitorApp:
    def __init__(self, root):
        self.root = root
        self.config = load_config()
        self.services = self.config["services"]
        self.settings = self.config["settings"]
        self.cards = []          # list of dicts: canvas + svc + latest result
        self.row_state = {}      # idx(str) -> last state
        self.refreshing = False
        self._timer_id = None
        self._pending = 0
        self._reflow_job = None
        self._last_run = {}      # idx(int) -> monotonic time of last check
        self.selected = None     # selected card index (str)

        root.title(APP_NAME)
        root.geometry("780x600")
        root.minsize(560, 420)
        root.configure(bg=BG)

        self._build_header()
        self._build_cards_area()
        self._build_controls()
        self._build_statusbar()

        self.populate_cards()
        self.root.after(60, self.reflow)
        self.refresh_now()
        self.schedule_next()

    # -- header --
    def _build_header(self):
        bar = tk.Frame(self.root, bg=BG)
        bar.pack(fill="x", padx=22, pady=(20, 6))
        left = tk.Frame(bar, bg=BG)
        left.pack(side="left", anchor="w")
        tk.Label(left, text="MEDALERT \u2022 STATUS", bg=BG, fg=ACCENT,
                 font=(FONT, 9, "bold")).pack(anchor="w")
        tk.Label(left, text="Service Health", bg=BG, fg=TEXT,
                 font=(FONT, 22, "bold")).pack(anchor="w")

        right = tk.Frame(bar, bg=BG)
        right.pack(side="right", anchor="e")
        self.summary_var = tk.StringVar(value="")
        self.summary_lbl = tk.Label(right, textvariable=self.summary_var, bg=BG,
                                    fg=MUTED, font=(FONT, 11, "bold"))
        self.summary_lbl.pack(anchor="e")

    # -- scrollable card grid --
    def _build_cards_area(self):
        wrap = tk.Frame(self.root, bg=BG)
        wrap.pack(fill="both", expand=True, padx=15, pady=4)
        self.canvas = tk.Canvas(wrap, bg=BG, highlightthickness=0, bd=0)
        self.canvas.pack(side="left", fill="both", expand=True)
        self.vsb = tk.Scrollbar(wrap, orient="vertical", command=self.canvas.yview,
                                bg=PANEL, troughcolor=BG, bd=0,
                                activebackground=ACCENT_STRONG,
                                highlightthickness=0)
        self.vsb.pack(side="right", fill="y")
        self.canvas.configure(yscrollcommand=self.vsb.set)

        self.host = tk.Frame(self.canvas, bg=BG)
        self._host_id = self.canvas.create_window((0, 0), window=self.host,
                                                  anchor="nw")
        self.host.bind("<Configure>",
                       lambda _e: self.canvas.configure(
                           scrollregion=self.canvas.bbox("all")))
        self.canvas.bind("<Configure>", self._on_canvas_configure)
        self.canvas.bind_all("<MouseWheel>", self._on_wheel)

    def _on_wheel(self, event):
        self.canvas.yview_scroll(int(-event.delta / 120), "units")

    def _on_canvas_configure(self, event):
        self.canvas.itemconfigure(self._host_id, width=event.width)
        if self._reflow_job:
            self.root.after_cancel(self._reflow_job)
        self._reflow_job = self.root.after(40, self.reflow)

    # -- controls --
    def _button(self, parent, text, command, primary=False):
        bg = ACCENT_STRONG if primary else "#1b2447"
        active = ACCENT if primary else "#243056"
        fg = "white" if primary else TEXT
        btn = tk.Button(parent, text=text, command=command, bg=bg, fg=fg,
                        activebackground=active, activeforeground="white",
                        relief="flat", bd=0, padx=16, pady=7,
                        font=(FONT, 10, "bold"), cursor="hand2",
                        highlightthickness=0)
        return btn

    def _build_controls(self):
        bar = tk.Frame(self.root, bg=BG)
        bar.pack(fill="x", padx=22, pady=(6, 8))

        self.check_btn = self._button(bar, "Check now", self.refresh_now, primary=True)
        self.check_btn.pack(side="left")

        self.auto_var = tk.BooleanVar(value=bool(self.settings.get("auto_refresh", True)))
        chk_kw = dict(bg=BG, fg=MUTED, activebackground=BG, activeforeground=TEXT,
                      selectcolor=CARD_BG, relief="flat", highlightthickness=0,
                      font=(FONT, 10))
        tk.Checkbutton(bar, text="Auto", variable=self.auto_var,
                       command=self.on_auto_toggle, **chk_kw).pack(side="left", padx=(12, 2))

        self.interval_var = tk.StringVar(value=self.settings.get("interval_label", "30 sec"))
        opt = tk.OptionMenu(bar, self.interval_var, *INTERVAL_CHOICES.keys(),
                            command=lambda _v: self.on_interval_change())
        opt.configure(bg="#1b2447", fg=TEXT, activebackground="#243056",
                      activeforeground="white", relief="flat", bd=0,
                      highlightthickness=0, font=(FONT, 10), cursor="hand2",
                      width=6)
        opt["menu"].configure(bg=PANEL, fg=TEXT, activebackground=ACCENT_STRONG,
                              activeforeground="white", relief="flat")
        opt.pack(side="left", padx=(4, 0))

        self.sound_var = tk.BooleanVar(value=bool(self.settings.get("sound_on_failure", True)))
        tk.Checkbutton(bar, text="Beep on failure", variable=self.sound_var,
                       command=self.persist_settings, **chk_kw).pack(side="left", padx=(12, 0))

        self._button(bar, "Remove", self.remove_selected).pack(side="right")
        self._button(bar, "Add", self.add_service).pack(side="right", padx=(0, 8))

    def _build_statusbar(self):
        self.status_var = tk.StringVar(value="Ready.")
        tk.Label(self.root, textvariable=self.status_var, bg=PANEL, fg=MUTED,
                 anchor="w", padx=12, pady=5, font=(FONT, 9)).pack(fill="x", side="bottom")

    # -- card lifecycle --
    def populate_cards(self):
        for card in self.cards:
            card["canvas"].destroy()
        self.cards.clear()
        self.row_state.clear()
        self._last_run.clear()
        self.selected = None
        for idx, svc in enumerate(self.services):
            cv = tk.Canvas(self.host, bg=BG, height=CARD_H, highlightthickness=0, bd=0)
            cv.bind("<Button-1>", lambda _e, i=str(idx): self.select_card(i))
            self.cards.append({"canvas": cv, "svc": svc, "result": new_result()})

    def reflow(self):
        self._reflow_job = None
        if not self.cards:
            return
        width = self.canvas.winfo_width()
        if width <= 1:
            self.root.after(50, self.reflow)
            return
        cols = max(1, (width - GAP) // (CARD_MIN_W + GAP))
        cols = min(cols, max(1, len(self.cards)))
        card_w = (width - GAP * (cols + 1)) // cols
        for c in range(cols):
            self.host.grid_columnconfigure(c, weight=1)
        for i, card in enumerate(self.cards):
            cv = card["canvas"]
            cv.configure(width=card_w)
            cv.grid(row=i // cols, column=i % cols,
                    padx=GAP // 2, pady=GAP // 2, sticky="n")
            self.draw_card(card, str(i), card_w)

    def draw_card(self, card, idx, width):
        cv = card["canvas"]
        cv.delete("all")
        res = card["result"]
        state = res["state"]
        w, h = width, CARD_H
        selected = (self.selected == idx)
        border = ACCENT if selected else CARD_BORDER
        round_rect(cv, 2, 2, w - 2, h - 2, 18, fill=CARD_BG,
                   outline=border, width=2 if selected else 1)

        name = card["svc"].get("name", "?")
        cv.create_text(20, 22, text=elide(name, max(10, int(width / 9))),
                       anchor="w", fill=TEXT, font=(FONT, 12, "bold"))
        cv.create_text(20, 43, text=elide(target_label(card["svc"]),
                                          max(12, int(width / 6.5))),
                       anchor="w", fill=MUTED, font=(FONT, 8))

        # Status pill (top-right).
        pill_fg, pill_fill, pill_border = PILL.get(state, PILL["CHECKING"])
        pill_text = {"UP": "UP", "WARN": "WARN", "DOWN": "DOWN"}.get(state, "\u2026")
        pw = 26 + len(pill_text) * 8
        px2, py1, py2 = w - 16, 14, 36
        px1 = px2 - pw
        round_rect(cv, px1, py1, px2, py2, 11, fill=pill_fill, outline=pill_border)
        cv.create_oval(px1 + 11, (py1 + py2) // 2 - 3, px1 + 17,
                       (py1 + py2) // 2 + 3, fill=pill_fg, outline="")
        cv.create_text((px1 + px2) // 2 + 5, (py1 + py2) // 2, text=pill_text,
                       fill=pill_fg, font=(FONT, 9, "bold"))

        # Big value.
        cv.create_text(20, 80, text=res.get("value", "\u2014"), anchor="w",
                       fill=VALUE_COLOR.get(state, TEXT), font=(FONT, 21, "bold"))

        checked = res.get("checked", "")
        meta = res["detail"] if not checked else f"{res['detail']}  \u2022  {checked}"
        cv.create_text(20, h - 18, text=elide(meta, max(14, int(width / 6.5))),
                       anchor="w", fill=MUTED, font=(FONT, 8))

    def select_card(self, idx):
        prev = self.selected
        self.selected = None if prev == idx else idx
        for i, card in enumerate(self.cards):
            if str(i) in (prev, self.selected):
                self.draw_card(card, str(i), card["canvas"].winfo_width())

    # -- refresh flow --
    def refresh_now(self):
        self.refresh(force=True)

    def refresh(self, force):
        if self.refreshing or not self.services:
            return
        now = time.monotonic()
        to_run = []
        for i, card in enumerate(self.cards):
            min_interval = int(card["svc"].get("min_interval", 0))
            last = self._last_run.get(i, 0)
            if force or min_interval <= 0 or (now - last) >= min_interval:
                to_run.append(i)
        if not to_run:
            return
        self.refreshing = True
        self.check_btn.configure(state="disabled")
        self.status_var.set("Checking services\u2026")
        self._pending = len(to_run)
        for i in to_run:
            self._last_run[i] = now
            card = self.cards[i]
            card["result"] = new_result()
            self.draw_card(card, str(i), card["canvas"].winfo_width())
            threading.Thread(target=self._worker, args=(i, card["svc"]),
                             daemon=True).start()

    def _worker(self, idx, svc):
        result = run_check(svc)
        self.root.after(0, self._apply_result, idx, result)

    def _apply_result(self, idx, result):
        if idx >= len(self.cards):
            return
        result["checked"] = time.strftime("%H:%M:%S")
        card = self.cards[idx]
        card["result"] = result
        self.draw_card(card, str(idx), card["canvas"].winfo_width())

        key = str(idx)
        prev = self.row_state.get(key)
        self.row_state[key] = result["state"]
        if result["state"] == "DOWN" and prev not in (None, "DOWN") and self.sound_var.get():
            self.beep()

        self._pending -= 1
        if self._pending <= 0:
            self.finish_refresh()

    def finish_refresh(self):
        self.refreshing = False
        self.check_btn.configure(state="normal")
        up = sum(1 for s in self.row_state.values() if s == "UP")
        warn = sum(1 for s in self.row_state.values() if s == "WARN")
        down = sum(1 for s in self.row_state.values() if s == "DOWN")
        total = len(self.services)
        text = f"Up {up}/{total}"
        if warn:
            text += f"   \u2022 warn {warn}"
        if down:
            text += f"   \u2022 down {down}"
        self.summary_var.set(text)
        self.summary_lbl.configure(fg=DANGER if down else (WARN if warn else SUCCESS))
        self.status_var.set(f"Last checked {time.strftime('%H:%M:%S')}.")

    # -- scheduling --
    def schedule_next(self):
        if self._timer_id is not None:
            self.root.after_cancel(self._timer_id)
            self._timer_id = None
        if self.auto_var.get():
            secs = INTERVAL_CHOICES.get(self.interval_var.get(), 30)
            self._timer_id = self.root.after(secs * 1000, self._tick)

    def _tick(self):
        self.refresh(force=False)
        self.schedule_next()

    def on_auto_toggle(self):
        self.persist_settings()
        self.schedule_next()

    def on_interval_change(self):
        self.persist_settings()
        self.schedule_next()

    # -- add / remove --
    def add_service(self):
        name = simpledialog.askstring(APP_NAME, "Service name:", parent=self.root)
        if not name:
            return
        stype = simpledialog.askstring(
            APP_NAME, "Type (http, tcp, ping, netperf=upload, netdown=download):",
            parent=self.root, initialvalue="http")
        stype = (stype or "http").strip().lower()
        if stype not in CHECKERS:
            messagebox.showerror(APP_NAME, f"Unknown type: {stype}")
            return
        if stype == "http":
            target = simpledialog.askstring(APP_NAME, "URL (https://...):",
                                            parent=self.root)
            if not target:
                return
            svc = {"name": name, "type": "http", "target": target.strip(),
                   "expect_max": 399}
        elif stype == "tcp":
            target = simpledialog.askstring(APP_NAME, "Host / IP:", parent=self.root)
            if not target:
                return
            port = simpledialog.askinteger(APP_NAME, "Port:", parent=self.root,
                                           initialvalue=22, minvalue=1, maxvalue=65535)
            if not port:
                return
            svc = {"name": name, "type": "tcp", "target": target.strip(), "port": port}
        elif stype in ("netperf", "netdown"):
            target = simpledialog.askstring(APP_NAME, "Ping host (e.g. 1.1.1.1):",
                                            parent=self.root, initialvalue="1.1.1.1")
            if not target:
                return
            svc = {"name": name, "type": stype, "target": target.strip(),
                   "min_interval": 300}
        else:  # ping
            target = simpledialog.askstring(APP_NAME, "Host / IP:", parent=self.root)
            if not target:
                return
            svc = {"name": name, "type": "ping", "target": target.strip()}

        self.services.append(svc)
        self.save_and_reload()

    def remove_selected(self):
        if self.selected is None:
            messagebox.showinfo(APP_NAME, "Click a card to select it, then Remove.")
            return
        idx = int(self.selected)
        name = self.services[idx].get("name", "?")
        if messagebox.askyesno(APP_NAME, f"Remove '{name}'?"):
            del self.services[idx]
            self.save_and_reload()

    def save_and_reload(self):
        self.persist_settings()
        self.populate_cards()
        self.reflow()
        self.refresh_now()

    # -- persistence / misc --
    def persist_settings(self):
        self.settings["interval_label"] = self.interval_var.get()
        self.settings["auto_refresh"] = bool(self.auto_var.get())
        self.settings["sound_on_failure"] = bool(self.sound_var.get())
        self.config["services"] = self.services
        self.config["settings"] = self.settings
        save_config(self.config)

    def beep(self):
        try:
            if winsound is not None:
                winsound.MessageBeep(winsound.MB_ICONHAND)
            else:
                self.root.bell()
        except Exception:  # noqa: BLE001
            pass


def main():
    root = tk.Tk()
    app = MonitorApp(root)

    def on_close():
        app.persist_settings()
        root.destroy()

    root.protocol("WM_DELETE_WINDOW", on_close)
    root.mainloop()


if __name__ == "__main__":
    main()
