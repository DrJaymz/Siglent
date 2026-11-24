"""Live screen viewer and remote front panel for Siglent SDS1000X-E oscilloscopes
(tested on SDS1202X-E).

Talks raw SCPI over TCP port 5025. Needs only Pillow and numpy.

    python siglent_viewer.py [ip-address]
"""
import ctypes
import io
import json
import math
import os
import queue
import re
import socket
import struct
import sys
import threading
import time
import tkinter as tk
from datetime import datetime
from pathlib import Path
from tkinter import filedialog, messagebox, ttk

import numpy as np
from PIL import Image, ImageTk

__version__ = "1.1"

DEFAULT_IP = "192.168.0.202"
PORT = 5025
APP_DIR = Path(__file__).resolve().parent
SETTINGS_FILE = APP_DIR / "siglent_viewer.json"
CAPTURE_DIR = APP_DIR / "captures"
SETUP_DIR = APP_DIR / "setups"
LOG_FILE = APP_DIR / "siglent_viewer.log"
GRID_DIVS = 14          # horizontal divisions on the SDS1000X-E
CODES_PER_DIV = 25      # waveform ADC codes per vertical division
SETTLE_TIME = 0.25      # seconds to leave the scope alone after a settings change
CHANNELS = ("C1", "C2")
MEASUREMENTS = [        # (PAVA key, label)
    ("PKPK", "Vpp"), ("MAX", "Max"), ("MIN", "Min"), ("MEAN", "Mean"),
    ("RMS", "RMS"), ("FREQ", "Freq"), ("PER", "Period"), ("RISE", "Rise"),
    ("FALL", "Fall"), ("DUTY", "Duty"),
]
COUPLINGS = {"D1M": "DC", "A1M": "AC", "GND": "GND", "D50": "DC 50Ω", "A50": "AC 50Ω"}

# colours
BG = "#16191d"          # window
PANEL = "#1e2227"       # control panel
BTN = "#2c3239"
BTN_HOT = "#3a424c"
EDGE = "#3d444d"
FG = "#d0d7de"
DIM = "#8b949e"
CH_COLOUR = {"C1": "#f5e04a", "C2": "#ff5fd2"}
GREEN = "#2ea043"
RED = "#da3633"
BLUE = "#3b82f6"


class ScopeError(Exception):
    pass


_log_lock = threading.Lock()


def log(message):
    """Append a timestamped line to the flight-recorder log."""
    try:
        with _log_lock:
            if LOG_FILE.exists() and LOG_FILE.stat().st_size > 2_000_000:
                LOG_FILE.replace(LOG_FILE.with_suffix(".old.log"))
            with LOG_FILE.open("a", encoding="utf-8") as f:
                f.write(f"{datetime.now():%Y-%m-%d %H:%M:%S}  {message}\n")
    except OSError:
        pass


def value_of(resp):
    """'C1:VDIV 1.00E+00V' -> '1.00E+00V' (works with CHDR SHORT, LONG or OFF)."""
    resp = resp.strip()
    return resp.split(" ", 1)[1] if " " in resp else resp


def number_of(resp):
    m = re.match(r"\s*([-+]?\d*\.?\d+(?:[eE][-+]?\d+)?)", value_of(resp))
    return float(m.group(1)) if m else None


def eng(value, unit):
    """Format with an SI prefix: 1.5e-6, 's' -> '1.5 µs'."""
    if value is None:
        return "—"
    if value == 0:
        return f"0 {unit}"
    for factor, prefix in ((1e9, "G"), (1e6, "M"), (1e3, "k"), (1, ""),
                           (1e-3, "m"), (1e-6, "µ"), (1e-9, "n"), (1e-12, "p")):
        if abs(value) >= factor * 0.9995:
            return f"{value / factor:.4g} {prefix}{unit}"
    return f"{value:.3g} {unit}"


def eng_raw(raw):
    """Format a scope measurement string such as '8.59E-07S' or '****'."""
    m = re.match(r"([-+0-9.eE]+)(.*)", raw)
    try:
        return eng(float(m.group(1)), "s" if m.group(2) == "S" else m.group(2)) if m else "—"
    except ValueError:
        return "—"


def step_125(value, direction):
    """Next value up (+1) or down (-1) in the 1-2-5 sequence."""
    exp = math.floor(math.log10(value) + 1e-9)
    mant = value / 10 ** exp
    idx = min(range(3), key=lambda i: abs((1, 2, 5)[i] - mant)) + direction
    exp += idx // 3
    return (1, 2, 5)[idx % 3] * 10.0 ** exp


class Scope:
    """Blocking SCPI connection. Used only from the worker thread."""

    def __init__(self, host):
        self.sock = socket.create_connection((host, PORT), timeout=3)
        self.sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
        self.sock.setsockopt(socket.SOL_SOCKET, socket.SO_RCVBUF, 1 << 21)
        self._quiet_until = 0.0
        self.last_cmd = ""

    def close(self):
        """Close politely (FIN, not reset): the scope copes badly with resets."""
        try:
            self.sock.shutdown(socket.SHUT_RDWR)
        except OSError:
            pass
        try:
            self.sock.close()
        except OSError:
            pass

    def drain(self, idle=0.5):
        """Discard incoming data until the line has been quiet for `idle` s."""
        self.sock.settimeout(idle)
        count = 0
        try:
            while True:
                chunk = self.sock.recv(1 << 20)
                if not chunk:
                    raise ScopeError("connection closed by scope")
                count += len(chunk)
        except socket.timeout:
            return count

    def send(self, cmd):
        self.last_cmd = cmd
        # The scope silently discards anything that arrives within ~0.1 s of a
        # settings change, so hold off after every non-query command.
        wait = self._quiet_until - time.monotonic()
        if wait > 0:
            time.sleep(wait)
        self.sock.sendall(cmd.encode("ascii", "replace") + b"\n")
        if "?" not in cmd and cmd != "SCDP":
            slow = cmd.upper().startswith(("ASET", "*RST", "AUTO_SETUP"))
            self._quiet_until = time.monotonic() + (3.0 if slow else SETTLE_TIME)

    def _read_exact(self, n, timeout=5):
        self.sock.settimeout(timeout)
        buf = bytearray(n)
        view = memoryview(buf)
        got = 0
        while got < n:
            k = self.sock.recv_into(view[got:], n - got)
            if not k:
                raise ScopeError("connection closed by scope")
            got += k
        return bytes(buf)

    def query(self, cmd, timeout=3):
        self.send(cmd)
        self.sock.settimeout(timeout)
        data = b""
        while not data.endswith(b"\n"):
            chunk = self.sock.recv(65536)
            if not chunk:
                raise ScopeError("connection closed by scope")
            data += chunk
        return data.decode("latin-1").strip()

    def screenshot(self):
        """Return the screen as BMP file bytes."""
        self.send("SCDP")
        head = self._read_exact(6)
        if head[:2] != b"BM":
            raise ScopeError("unexpected screenshot data")
        size = struct.unpack("<I", head[2:6])[0]
        body = self._read_exact(size - 6 + 1)       # +1: trailing newline
        return head + body[:-1]

    def _read_block(self, timeout):
        """Read a '#9nnnnnnnnn<data>' block. Returns (data, bytes read past it)."""
        self.sock.settimeout(timeout)
        head = b""
        while True:
            i = head.find(b"#9")
            if i >= 0 and len(head) >= i + 11:
                break
            chunk = self.sock.recv(64)
            if not chunk:
                raise ScopeError("connection closed by scope")
            head += chunk
            if len(head) > 4096:
                raise ScopeError("bad block header")
        n = int(head[i + 2:i + 11])
        data = head[i + 11:]
        if len(data) < n:
            data += self._read_exact(n - len(data), timeout=30)
        return data[:n], data[n:]

    def read_setup(self):
        """Return the scope's complete setup (an XML document) as bytes."""
        self.send("PNSU?")
        xml, _ = self._read_block(timeout=15)       # takes ~3.5 s to start
        # this firmware appends a few kB of junk after the block: discard it
        self.sock.settimeout(0.5)
        try:
            while self.sock.recv(65536):
                pass
        except socket.timeout:
            pass
        return xml

    def write_setup(self, xml):
        wait = self._quiet_until - time.monotonic()
        if wait > 0:
            time.sleep(wait)
        self.sock.sendall(b"PNSU #9%09d" % len(xml) + xml + b"\n")
        self._quiet_until = time.monotonic() + 2.5  # scope is busy applying it

    def waveform(self, channel):
        """Return (time_s, volts) numpy arrays; time is relative to the trigger."""
        vdiv = number_of(self.query(f"{channel}:VDIV?"))
        offset = number_of(self.query(f"{channel}:OFST?"))
        tdiv = number_of(self.query("TDIV?"))
        trdl = number_of(self.query("TRDL?"))
        sara = number_of(self.query("SARA?"))
        self.send(f"{channel}:WF? DAT2")
        raw, extra = self._read_block(timeout=10)
        if len(extra) < 2:                          # block is followed by "\n\n"
            self._read_exact(2 - len(extra))
        n = len(raw)
        codes = np.frombuffer(raw, dtype=np.int8).astype(np.float64)
        volts = codes * (vdiv / CODES_PER_DIV) - offset
        # screen centre is at -TRDL relative to the trigger
        t = -trdl - (tdiv * GRID_DIVS / 2) + np.arange(n) / sara
        return t, volts


class Worker(threading.Thread):
    """Owns the socket: streams frames, runs queued jobs, polls scope state."""

    def __init__(self, host, ui_queue):
        super().__init__(daemon=True)
        self.host = host
        self.ui_queue = ui_queue          # (callback, args) for the UI thread
        self.jobs = queue.Queue()         # (fn(scope) -> result, callback or None)
        self.jobs_done = 0                # jobs taken off the queue so far
        self.streaming = True
        self.next_poll = 0.0              # set to 0 to poll immediately
        self.frame = None                 # (sequence number, PIL image)
        self.fps = 0.0
        self.on_status = None             # callback(text, connected)
        self.on_poll = None               # callback(dict)
        self._reconnect = threading.Event()
        self._stop = False
        self._seq = 0

    def set_host(self, host):
        self.host = host
        self._reconnect.set()

    def submit(self, fn, callback=None):
        self.jobs.put((fn, callback))

    def _ui(self, callback, *args):
        if callback:
            self.ui_queue.put((callback, args))

    def stop(self):
        """Ask the thread to finish its current request and close cleanly."""
        self._stop = True
        self._reconnect.set()

    def run(self):
        failures = 0        # consecutive short-lived or failed connections
        while not self._stop:
            self._reconnect.clear()
            host = self.host
            self._ui(self.on_status, f"Connecting to {host}…", False)
            scope = None
            try:
                scope = Scope(host)
                idn = scope.query("*IDN?")
            except (OSError, ScopeError) as e:
                if scope:
                    scope.close()
                failures += 1
                log(f"connect to {host} failed: {e!r}")
                self._ui(self.on_status, f"Cannot reach {host}: {e}", False)
                self._drop_jobs()
                self._reconnect.wait(min(15, 2 * failures))
                continue
            log(f"connected to {host}: {idn}")
            self._ui(self.on_status, idn.replace(",", "  "), True)
            started = time.monotonic()
            try:
                self._session(scope)
                log("connection closed by viewer")
            except (OSError, ScopeError, ValueError, TypeError) as e:
                # Reconnecting too eagerly is hard on the scope: back off if
                # connections keep failing soon after they open.
                failures = 0 if time.monotonic() - started > 30 else failures + 1
                wait = min(15, 1 + 2 * failures)
                log(f"CONNECTION LOST after {time.monotonic() - started:.0f} s, last command "
                    f"{scope.last_cmd!r}: {e!r}; retrying in {wait} s")
                self._ui(self.on_status, f"Connection lost: {e}", False)
                self._reconnect.wait(wait)
            finally:
                scope.close()

    def _drop_jobs(self):
        while True:
            try:
                _, callback = self.jobs.get_nowait()
            except queue.Empty:
                return
            self.jobs_done += 1
            self._ui(callback, ScopeError("not connected"))

    def _session(self, scope):
        self.next_poll = 0.0
        times = []
        misses = 0                          # consecutive unanswered requests
        health = {"since": time.monotonic(), "frames": 0, "shot": 0.0,
                  "shot_max": 0.0, "poll_max": 0.0, "misses": 0}
        while not self._reconnect.is_set():
            try:
                self._step(scope, times, health)
                misses = 0
            except socket.timeout:
                # The scope missed a request. Dropping the connection and opening
                # another is hard on it, so resynchronise on this one instead.
                misses += 1
                health["misses"] += 1
                late = scope.drain()
                log(f"NO ANSWER #{misses} to {scope.last_cmd!r} "
                    f"({late} late bytes discarded)")
                if misses >= 3:
                    raise ScopeError(f"scope stopped answering ({scope.last_cmd})")
            if time.monotonic() - health["since"] >= 60:
                n = health["frames"]
                log(f"health: {n} frames, screenshot avg "
                    f"{health['shot'] / n * 1000 if n else 0:.0f} ms max "
                    f"{health['shot_max'] * 1000:.0f} ms, poll max "
                    f"{health['poll_max'] * 1000:.0f} ms, unanswered {health['misses']}")
                health.update(since=time.monotonic(), frames=0, shot=0.0,
                              shot_max=0.0, poll_max=0.0, misses=0)

    def _step(self, scope, times, health):
        """One pass of the worker loop: queued jobs, a poll if due, one frame."""
        try:
            while True:
                fn, callback = self.jobs.get_nowait()
                self.jobs_done += 1
                try:
                    result = fn(scope)
                except socket.timeout:
                    self._ui(callback, ScopeError("no response (timeout)"))
                    raise
                self._ui(callback, result)
        except queue.Empty:
            pass
        if time.monotonic() >= self.next_poll:
            self.next_poll = time.monotonic() + 1.0
            t = time.monotonic()
            self._ui(self.on_poll, self._poll(scope))
            health["poll_max"] = max(health["poll_max"], time.monotonic() - t)
        if self.streaming:
            t = time.monotonic()
            bmp = scope.screenshot()
            taken = time.monotonic() - t
            health["frames"] += 1
            health["shot"] += taken
            health["shot_max"] = max(health["shot_max"], taken)
            if taken > 1.5:
                log(f"slow screenshot: {taken:.1f} s")
            img = Image.open(io.BytesIO(bmp))
            img.load()
            self._seq += 1
            self.frame = (self._seq, img.convert("RGB"))
            times.append(time.monotonic())
            del times[:-15]
            if len(times) > 1:
                self.fps = (len(times) - 1) / (times[-1] - times[0])
        else:
            self.fps = 0.0
            times.clear()
            time.sleep(0.03)

    def _poll(self, scope):
        q = scope.query
        info = {
            "jobs_done": self.jobs_done,
            "status": value_of(q("SAST?")),
            "mode": value_of(q("TRMD?")),
            "tdiv": number_of(q("TDIV?")),
            "trdl": number_of(q("TRDL?")),
            "channels": {},
            "trig": {"src": None, "level": None, "slope": None},
        }
        for ch in CHANNELS:
            on = value_of(q(f"{ch}:TRA?")) == "ON"
            pava = None
            if on:
                parts = value_of(q(f"{ch}:PAVA? ALL")).split(",")
                pava = dict(zip(parts[0::2], parts[1::2]))
            info["channels"][ch] = {
                "on": on,
                "vdiv": number_of(q(f"{ch}:VDIV?")),
                "ofst": number_of(q(f"{ch}:OFST?")),
                "cpl": value_of(q(f"{ch}:CPL?")),
                "pava": pava,
            }
        fields = value_of(q("TRSE?")).split(",")
        if "SR" in fields[:-1]:
            src = fields[fields.index("SR") + 1]
            info["trig"]["src"] = src
            if src in ("C1", "C2", "EX", "EX5"):
                info["trig"]["level"] = number_of(q(f"{src}:TRLV?"))
                info["trig"]["slope"] = value_of(q(f"{src}:TRSL?"))
        return info


def copy_image_to_clipboard(img):
    buf = io.BytesIO()
    img.convert("RGB").save(buf, "BMP")
    data = buf.getvalue()[14:]            # DIB = BMP without the file header
    k32 = ctypes.WinDLL("kernel32")
    u32 = ctypes.WinDLL("user32")
    k32.GlobalAlloc.restype = ctypes.c_void_p
    k32.GlobalAlloc.argtypes = (ctypes.c_uint, ctypes.c_size_t)
    k32.GlobalLock.restype = ctypes.c_void_p
    k32.GlobalLock.argtypes = (ctypes.c_void_p,)
    k32.GlobalUnlock.argtypes = (ctypes.c_void_p,)
    u32.OpenClipboard.argtypes = (ctypes.c_void_p,)
    u32.SetClipboardData.restype = ctypes.c_void_p
    u32.SetClipboardData.argtypes = (ctypes.c_uint, ctypes.c_void_p)
    handle = k32.GlobalAlloc(0x0002, len(data))          # GMEM_MOVEABLE
    ptr = k32.GlobalLock(handle)
    ctypes.memmove(ptr, data, len(data))
    k32.GlobalUnlock(handle)
    if not u32.OpenClipboard(None):
        raise OSError("clipboard is busy")
    try:
        u32.EmptyClipboard()
        u32.SetClipboardData(8, handle)                  # CF_DIB
    finally:
        u32.CloseClipboard()


def apply_dark_theme(root):
    root.configure(bg=BG)
    style = ttk.Style(root)
    style.theme_use("clam")
    style.configure(".", background=BG, foreground=FG, font=("Segoe UI", 9),
                    bordercolor=EDGE, lightcolor=BTN, darkcolor=BTN, focuscolor=BTN)
    style.configure("TFrame", background=BG)
    style.configure("Panel.TFrame", background=PANEL)
    style.configure("TLabel", background=BG, foreground=FG)
    style.configure("TSeparator", background=EDGE)
    style.configure("TEntry", fieldbackground="#0d1014", foreground=FG,
                    insertcolor=FG, bordercolor=EDGE, lightcolor=EDGE, darkcolor=EDGE)
    style.map("TEntry", fieldbackground=[("readonly", BG)],
              bordercolor=[("focus", BLUE)], lightcolor=[("focus", BLUE)],
              darkcolor=[("focus", BLUE)])

    def button_style(name, bg, fg, hot):
        style.configure(name, background=bg, foreground=fg, padding=(6, 3),
                        relief="flat", lightcolor=bg, darkcolor=bg, focuscolor=bg)
        style.map(name, background=[("pressed", EDGE), ("active", hot)],
                  lightcolor=[("active", hot)], darkcolor=[("active", hot)])

    button_style("TButton", BTN, FG, BTN_HOT)
    button_style("Sel.TButton", BLUE, "#ffffff", "#5b9bff")
    button_style("Go.TButton", GREEN, "#ffffff", "#3fb950")
    button_style("Halt.TButton", RED, "#ffffff", "#f85149")
    for ch, colour in CH_COLOUR.items():
        button_style(f"{ch}on.TButton", colour, "#000000", colour)
        button_style(f"{ch}off.TButton", BTN, colour, BTN_HOT)
    for name in ("Sel", "Go", "Halt", "C1on", "C2on", "C1off", "C2off"):
        style.configure(f"{name}.TButton", font=("Segoe UI", 9, "bold"))


def dark_title_bar(root):
    try:
        root.update_idletasks()
        user32 = ctypes.windll.user32
        user32.GetParent.restype = ctypes.c_void_p
        user32.GetParent.argtypes = (ctypes.c_void_p,)
        hwnd = user32.GetParent(root.winfo_id())
        value = ctypes.c_int(1)
        ctypes.windll.dwmapi.DwmSetWindowAttribute(      # DWMWA_USE_IMMERSIVE_DARK_MODE
            ctypes.c_void_p(hwnd), 20, ctypes.byref(value), ctypes.sizeof(value))
    except (AttributeError, OSError):
        pass


class App:
    def __init__(self, root, host):
        self.root = root
        self.settings = self._load_settings()
        host = host or self.settings.get("ip", DEFAULT_IP)
        root.title(f"Siglent Viewer {__version__}")
        apply_dark_theme(root)
        self.ui_queue = queue.Queue()
        self.worker = Worker(host, self.ui_queue)
        self.worker.on_status = self._on_status
        self.worker.on_poll = self._on_poll
        self.image = None           # native-resolution PIL image on display
        self.shown = (None, None)   # (frame seq, canvas size) currently drawn
        self.photo = None
        self.message_until = 0.0
        self.connected_text = ""
        self.state = None           # latest scope settings (see Worker._poll)
        self.jobs_submitted = 0
        self.run_mode = "AUTO"      # trigger mode to return to after Stop

        self._build(host)
        self._refresh()
        root.bind("<F5>", lambda _: self.toggle_stream())
        root.bind("<Control-s>", lambda _: self.quick_save())
        root.bind("<Control-Shift-S>", lambda _: self.snapshot())
        root.bind("<Control-c>", self._copy_key)
        root.protocol("WM_DELETE_WINDOW", self._close)
        self.worker.start()
        self._tick()

    # ---------- UI construction ----------
    def _build(self, host):
        self._build_toolbar(host)

        status = ttk.Frame(self.root, padding=(6, 2))
        status.pack(side="bottom", fill="x")
        self.status_var = tk.StringVar(value="Starting…")
        self.fps_var = tk.StringVar()
        ttk.Label(status, textvariable=self.status_var, foreground=DIM).pack(side="left")
        ttk.Label(status, textvariable=self.fps_var, width=10, anchor="e",
                  foreground=DIM).pack(side="right")

        scpi = ttk.Frame(self.root, padding=(6, 2))
        scpi.pack(side="bottom", fill="x")
        ttk.Label(scpi, text="SCPI").pack(side="left")
        self.scpi_var = tk.StringVar()
        entry = ttk.Entry(scpi, textvariable=self.scpi_var, width=28)
        entry.pack(side="left", padx=6)
        entry.bind("<Return>", lambda _: self.send_scpi())
        ttk.Button(scpi, text="Send", command=self.send_scpi).pack(side="left")
        self.reply_var = tk.StringVar(value="type a command, e.g.  C1:VDIV?  or  TDIV 1MS")
        ttk.Entry(scpi, textvariable=self.reply_var, state="readonly").pack(
            side="left", fill="x", expand=True, padx=6)

        self._build_panel()
        self._build_measurements()

        self.canvas = tk.Canvas(self.root, width=800, height=480, bg="black",
                                highlightthickness=0)
        self.canvas.pack(side="left", fill="both", expand=True)
        self.canvas_image = self.canvas.create_image(0, 0, anchor="center")

    def _build_toolbar(self, host):
        bar = ttk.Frame(self.root, padding=(6, 5))
        bar.pack(side="top", fill="x")

        def button(text, command, width=None):
            b = ttk.Button(bar, text=text, command=command, width=width)
            b.pack(side="left", padx=2)
            return b

        def separator():
            ttk.Separator(bar, orient="vertical").pack(side="left", fill="y", padx=6)

        self.ip_var = tk.StringVar(value=host)
        ip = ttk.Entry(bar, textvariable=self.ip_var, width=15)
        ip.pack(side="left", padx=2)
        ip.bind("<Return>", lambda _: self.connect())
        button("Connect", self.connect)
        separator()
        self.stream_btn = button("Pause  (F5)", self.toggle_stream, width=12)
        button("Snapshot…", self.snapshot)
        button("Quick save", self.quick_save)
        button("Copy", self.copy)
        separator()
        button("Save CSV…", self.save_csv)
        self.setups_btn = button("Setups ▾", self.show_setups_menu)
        self.setups_menu = tk.Menu(self.root, tearoff=False, bg=BTN, fg=FG, bd=0,
                                   activebackground=BLUE, activeforeground="#ffffff",
                                   font=("Segoe UI", 9))

    def _build_panel(self):
        """Right-hand control panel, laid out like the scope's front panel."""
        panel = ttk.Frame(self.root, style="Panel.TFrame", padding=(10, 8))
        panel.pack(side="right", fill="y")

        def section(title):
            tk.Label(panel, text=title, bg=PANEL, fg=DIM,
                     font=("Segoe UI", 8, "bold")).pack(anchor="w", pady=(10, 0))
            tk.Frame(panel, bg=EDGE, height=1).pack(fill="x", pady=(1, 6))
            frame = ttk.Frame(panel, style="Panel.TFrame")
            frame.pack(fill="x")
            return frame

        def readout(parent, colour=FG, size=11):
            var = tk.StringVar(value="—")
            label = tk.Label(parent, textvariable=var, bg=PANEL, fg=colour,
                             font=("Consolas", size, "bold"))
            return var, label

        def caption(parent, text):
            return tk.Label(parent, text=text, bg=PANEL, fg=DIM, font=("Segoe UI", 8))

        def keys(parent, *specs):
            """Row of small buttons from (text, command) pairs."""
            row = ttk.Frame(parent, style="Panel.TFrame")
            for text, command in specs:
                ttk.Button(row, text=text, width=max(3, len(text) + 1),
                           command=command).pack(side="left", padx=1)
            return row

        # --- Run/Stop and Auto Setup: top right, as on the scope
        top = ttk.Frame(panel, style="Panel.TFrame")
        top.pack(fill="x")
        self.run_btn = ttk.Button(top, text="Run / Stop", command=self.run_stop)
        self.run_btn.pack(side="left", fill="x", expand=True, padx=(0, 3), ipady=3)
        ttk.Button(top, text="Auto Setup", command=self.auto_setup).pack(
            side="left", fill="x", expand=True, padx=(3, 0), ipady=3)

        # --- Vertical: one column per channel
        vert = section("VERTICAL")
        self.ch_widgets = {}
        for col, ch in enumerate(CHANNELS):
            vert.columnconfigure(col, weight=1, uniform="ch")
            f = ttk.Frame(vert, style="Panel.TFrame")
            f.grid(row=0, column=col, sticky="n", padx=3)
            btn = ttk.Button(f, text=f"CH{ch[1]}", command=lambda c=ch: self.toggle_channel(c))
            btn.pack(fill="x", ipady=2)
            vdiv, label = readout(f, CH_COLOUR[ch])
            label.pack(pady=(6, 0))
            keys(f, ("▲", lambda c=ch: self.step_vdiv(c, +1)),
                 ("▼", lambda c=ch: self.step_vdiv(c, -1))).pack()
            caption(f, "position").pack(pady=(6, 0))
            pos, label = readout(f, CH_COLOUR[ch], 10)
            label.pack()
            keys(f, ("▲", lambda c=ch: self.step_offset(c, +1)),
                 ("0", lambda c=ch: self.step_offset(c, 0)),
                 ("▼", lambda c=ch: self.step_offset(c, -1))).pack()
            cpl = ttk.Button(f, text="—", command=lambda c=ch: self.cycle_coupling(c))
            cpl.pack(fill="x", pady=(6, 0))
            self.ch_widgets[ch] = {"btn": btn, "vdiv": vdiv, "pos": pos, "cpl": cpl}

        # --- Horizontal
        horiz = section("HORIZONTAL")
        horiz.columnconfigure(0, weight=1)
        self.tdiv_var, label = readout(horiz)
        label.grid(row=0, column=0, sticky="w")
        keys(horiz, ("◀", lambda: self.step_tdiv(-1)),
             ("▶", lambda: self.step_tdiv(+1))).grid(row=0, column=1, sticky="e")
        caption(horiz, "delay").grid(row=1, column=0, sticky="w", pady=(6, 0))
        self.delay_var, label = readout(horiz, size=10)
        label.grid(row=2, column=0, sticky="w")
        keys(horiz, ("◀", lambda: self.step_delay(-1)), ("0", lambda: self.step_delay(0)),
             ("▶", lambda: self.step_delay(+1))).grid(row=2, column=1, sticky="e")

        # --- Trigger
        trig = section("TRIGGER")
        trig.columnconfigure(0, weight=1)
        self.trig_status_var, label = readout(trig, "#58a6ff")
        label.grid(row=0, column=0, columnspan=2, sticky="w")
        modes = ttk.Frame(trig, style="Panel.TFrame")
        modes.grid(row=1, column=0, columnspan=2, sticky="ew", pady=(4, 0))
        self.mode_btns = {}
        for text, mode in (("Auto", "AUTO"), ("Normal", "NORM"), ("Single", "SINGLE")):
            b = ttk.Button(modes, text=text, width=7,
                           command=lambda m=mode: self.set_trigger_mode(m))
            b.pack(side="left", fill="x", expand=True, padx=1)
            self.mode_btns[mode] = b
        caption(trig, "level").grid(row=2, column=0, sticky="w", pady=(6, 0))
        self.level_var, self.level_label = readout(trig, size=10)
        self.level_label.grid(row=3, column=0, sticky="w")
        keys(trig, ("▲", lambda: self.step_level(+1)), ("50%", self.level_50),
             ("▼", lambda: self.step_level(-1))).grid(row=3, column=1, sticky="e")
        extra = ttk.Frame(trig, style="Panel.TFrame")
        extra.grid(row=4, column=0, columnspan=2, sticky="ew", pady=(6, 0))
        self.slope_btn = ttk.Button(extra, text="Slope", command=self.toggle_slope)
        self.slope_btn.pack(side="left", fill="x", expand=True, padx=1)
        ttk.Button(extra, text="Force", command=lambda: self.command("FRTR")).pack(
            side="left", fill="x", expand=True, padx=1)

    def _build_measurements(self):
        table = ttk.Frame(self.root, padding=(6, 4))
        table.pack(side="bottom", fill="x")
        self.meas_vars = {}
        tk.Label(table, text="", bg=BG, width=4).grid(row=0, column=0)
        for col, (_, label) in enumerate(MEASUREMENTS, start=1):
            table.columnconfigure(col, weight=1, uniform="m")
            tk.Label(table, text=label, bg=BG, fg=DIM,
                     font=("Segoe UI", 8)).grid(row=0, column=col, sticky="w")
        for row, ch in enumerate(CHANNELS, start=1):
            tk.Label(table, text=f"CH{ch[1]}", bg=BG, fg=CH_COLOUR[ch],
                     font=("Segoe UI", 9, "bold")).grid(row=row, column=0, sticky="w")
            for col, (key, _) in enumerate(MEASUREMENTS, start=1):
                var = tk.StringVar()
                tk.Label(table, textvariable=var, bg=BG, fg=CH_COLOUR[ch], anchor="w",
                         font=("Consolas", 10)).grid(row=row, column=col, sticky="w")
                self.meas_vars[ch, key] = var

    # ---------- settings ----------
    @staticmethod
    def _load_settings():
        try:
            return json.loads(SETTINGS_FILE.read_text())
        except (OSError, ValueError):
            return {}

    def _save_settings(self):
        try:
            SETTINGS_FILE.write_text(json.dumps(self.settings, indent=2))
        except OSError:
            pass

    # ---------- periodic UI update ----------
    def _tick(self):
        try:
            while True:
                callback, args = self.ui_queue.get_nowait()
                callback(*args)
        except queue.Empty:
            pass
        frame = self.worker.frame
        if frame and self.worker.streaming:
            self.image = frame[1]
            seq = frame[0]
        else:
            seq = self.shown[0]
        size = (self.canvas.winfo_width(), self.canvas.winfo_height())
        if self.image is not None and (seq, size) != self.shown and min(size) > 10:
            self._draw(size)
            self.shown = (seq, size)
        fps = self.worker.fps
        self.fps_var.set(f"{fps:.1f} fps" if fps else "paused" if not self.worker.streaming else "")
        self.root.after(15, self._tick)

    def _draw(self, size):
        img = self.image
        scale = min(size[0] / img.width, size[1] / img.height)
        if abs(scale - 1) > 0.01:
            new = (max(1, round(img.width * scale)), max(1, round(img.height * scale)))
            resample = Image.NEAREST if scale == int(scale) else Image.BILINEAR
            img = img.resize(new, resample)
        self.photo = ImageTk.PhotoImage(img)
        self.canvas.itemconfigure(self.canvas_image, image=self.photo)
        self.canvas.coords(self.canvas_image, size[0] // 2, size[1] // 2)

    def _on_status(self, text, connected):
        self.connected_text = text
        self.status_var.set(text)
        if not connected:
            self.state = None
            self._refresh()

    def _flash(self, text):
        """Show a message in the status bar for a few seconds."""
        self.status_var.set(text)
        token = self.message_until = time.monotonic() + 4
        self.root.after(4000, lambda: token == self.message_until and
                        self.status_var.set(self.connected_text))

    def _on_poll(self, info):
        if info["jobs_done"] < self.jobs_submitted:
            return          # taken before our latest command ran: stale
        self.state = info
        self._refresh()

    def _refresh(self):
        """Update every readout and button highlight from self.state."""
        st = self.state
        for ch in CHANNELS:
            w = self.ch_widgets[ch]
            c = st["channels"][ch] if st else None
            w["btn"].configure(style=f"{ch}on.TButton" if c and c["on"] else f"{ch}off.TButton")
            w["vdiv"].set(eng(c["vdiv"], "V") + "/div" if c else "—")
            w["pos"].set(eng(c["ofst"], "V") if c else "—")
            w["cpl"].configure(text=COUPLINGS.get(c["cpl"], c["cpl"]) if c else "—")
            for key, _ in MEASUREMENTS:
                pava = c["pava"] if c else None
                self.meas_vars[ch, key].set(eng_raw(pava.get(key, "****")) if pava else "")
        if not st:
            self.run_btn.configure(style="TButton")
            for var in (self.tdiv_var, self.delay_var, self.trig_status_var, self.level_var):
                var.set("—")
            for b in self.mode_btns.values():
                b.configure(style="TButton")
            self.slope_btn.configure(text="Slope")
            return
        stopped = st["mode"] == "STOP"
        if not stopped:
            self.run_mode = st["mode"]
        self.run_btn.configure(style="Halt.TButton" if stopped else "Go.TButton")
        self.tdiv_var.set(eng(st["tdiv"], "s") + "/div")
        self.delay_var.set(eng(-st["trdl"], "s"))       # shown as on the scope
        self.trig_status_var.set(st["status"])
        for mode, b in self.mode_btns.items():
            b.configure(style="Sel.TButton" if st["mode"] == mode else "TButton")
        trig = st["trig"]
        src = trig["src"]
        self.level_var.set(f"{src or '—'}  {eng(trig['level'], 'V')}")
        self.level_label.configure(fg=CH_COLOUR.get(src, FG))
        slope = {"POS": "Slope ↗", "NEG": "Slope ↘", "WINDOW": "Slope ↕"}
        self.slope_btn.configure(text=slope.get(trig["slope"], "Slope"))

    # ---------- scope controls ----------
    def command(self, cmd, quiet=False):
        """Send a command, then re-read the scope's settings straight away."""
        def done(result):
            self.worker.next_poll = 0.0
            if result is not None:
                self._flash(f"{cmd}: {result}")
            elif not quiet:
                self._flash(f"Sent {cmd}")

        self.jobs_submitted += 1
        self.worker.submit(lambda scope: scope.send(cmd), done)

    def _set(self, cmd):
        """Send a settings change whose effect is already shown optimistically."""
        self._refresh()
        self.command(cmd, quiet=True)

    def run_stop(self):
        if self.state:
            self.command("STOP" if self.state["mode"] != "STOP" else f"TRMD {self.run_mode}")

    def set_trigger_mode(self, mode):
        self.command(f"TRMD {mode}")

    def auto_setup(self):
        if messagebox.askokcancel("Auto setup", "Run Auto Setup? This changes the "
                                  "scope's vertical, horizontal and trigger settings.",
                                  parent=self.root):
            self.command("ASET")

    def toggle_channel(self, ch):
        if self.state:
            c = self.state["channels"][ch]
            c["on"] = not c["on"]
            self._set(f"{ch}:TRA {'ON' if c['on'] else 'OFF'}")

    def step_vdiv(self, ch, direction):
        if self.state:
            c = self.state["channels"][ch]
            c["vdiv"] = step_125(c["vdiv"], direction)
            self._set(f"{ch}:VDIV {c['vdiv']:.2E}V")

    def step_offset(self, ch, direction):
        """Move the trace up/down by a fifth of a division (0 = centre)."""
        if self.state:
            c = self.state["channels"][ch]
            c["ofst"] = c["ofst"] + direction * c["vdiv"] / 5 if direction else 0.0
            self._set(f"{ch}:OFST {c['ofst']:.4E}V")

    def cycle_coupling(self, ch):
        if self.state:
            c = self.state["channels"][ch]
            order = ["D1M", "A1M", "GND"]
            c["cpl"] = order[(order.index(c["cpl"]) + 1) % 3] if c["cpl"] in order else "D1M"
            self._set(f"{ch}:CPL {c['cpl']}")

    def step_tdiv(self, direction):
        if self.state:
            self.state["tdiv"] = min(100.0, max(1e-9, step_125(self.state["tdiv"], direction)))
            self._set(f"TDIV {self.state['tdiv']:.2E}S")

    def step_delay(self, direction):
        """Move the waveform left/right by half a division (0 = centre)."""
        if self.state:
            st = self.state
            st["trdl"] = st["trdl"] + direction * st["tdiv"] / 2 if direction else 0.0
            self._set(f"TRDL {st['trdl']:.4E}S")

    def step_level(self, direction):
        if not self.state or self.state["trig"]["level"] is None:
            return
        trig = self.state["trig"]
        src = self.state["channels"].get(trig["src"])
        trig["level"] += direction * (src["vdiv"] / 5 if src else 0.1)
        self._set(f"{trig['src']}:TRLV {trig['level']:.4E}V")

    def level_50(self):
        self.command("SET50")

    def toggle_slope(self):
        if self.state and self.state["trig"]["slope"]:
            trig = self.state["trig"]
            trig["slope"] = "NEG" if trig["slope"] == "POS" else "POS"
            self._set(f"{trig['src']}:TRSL {trig['slope']}")

    def send_scpi(self):
        cmd = self.scpi_var.get().strip()
        if not cmd:
            return
        self.jobs_submitted += 1
        if "?" in cmd:
            self.reply_var.set("…")
            self.worker.submit(lambda scope: scope.query(cmd),
                               lambda r: self.reply_var.set(str(r)))
        else:
            def done(result):
                self.worker.next_poll = 0.0
                self.reply_var.set("sent" if result is None else str(result))
            self.worker.submit(lambda scope: scope.send(cmd), done)

    # ---------- PC-side actions ----------
    def connect(self):
        host = self.ip_var.get().strip()
        if host:
            self.settings["ip"] = host
            self._save_settings()
            self.worker.set_host(host)

    def toggle_stream(self):
        self.worker.streaming = not self.worker.streaming
        self.stream_btn.configure(
            text="Pause  (F5)" if self.worker.streaming else "Resume  (F5)")

    def _stamp(self):
        return datetime.now().strftime("scope_%Y-%m-%d_%H-%M-%S")

    def snapshot(self):
        img = self.image                      # freeze the frame at click time
        if img is None:
            return self._flash("No image yet")
        path = filedialog.asksaveasfilename(
            parent=self.root, title="Save snapshot", defaultextension=".png",
            initialdir=self.settings.get("save_dir", str(CAPTURE_DIR.parent)),
            initialfile=self._stamp() + ".png",
            filetypes=[("PNG image", "*.png"), ("BMP image", "*.bmp"), ("JPEG image", "*.jpg")])
        if path:
            self._write_image(img, Path(path))
            self.settings["save_dir"] = str(Path(path).parent)
            self._save_settings()

    def quick_save(self):
        if self.image is None:
            return self._flash("No image yet")
        CAPTURE_DIR.mkdir(exist_ok=True)
        path = CAPTURE_DIR / (self._stamp() + ".png")
        n = 1
        while path.exists():
            n += 1
            path = CAPTURE_DIR / f"{self._stamp()}_{n}.png"
        self._write_image(self.image, path)

    def _write_image(self, img, path):
        try:
            img.save(path)
            self._flash(f"Saved {path}")
        except (OSError, ValueError) as e:
            messagebox.showerror("Save failed", str(e), parent=self.root)

    def _copy_key(self, event):
        if not isinstance(event.widget, (tk.Entry, ttk.Entry, tk.Text)):
            self.copy()

    def copy(self):
        if self.image is None:
            return self._flash("No image yet")
        try:
            copy_image_to_clipboard(self.image)
            self._flash("Screen copied to clipboard")
        except OSError as e:
            self._flash(f"Copy failed: {e}")

    def save_csv(self):
        path = filedialog.asksaveasfilename(
            parent=self.root, title="Save waveform data", defaultextension=".csv",
            initialdir=self.settings.get("save_dir", str(CAPTURE_DIR.parent)),
            initialfile=self._stamp() + ".csv", filetypes=[("CSV file", "*.csv")])
        if not path:
            return
        self.settings["save_dir"] = str(Path(path).parent)
        self._save_settings()
        self.status_var.set("Downloading waveform…")

        def fetch(scope):
            data = {}
            for ch in CHANNELS:
                if value_of(scope.query(f"{ch}:TRA?")) == "ON":
                    data[ch] = scope.waveform(ch)
            return data

        def done(data):
            if isinstance(data, Exception):
                return self._flash(f"Waveform download failed: {data}")
            if not data:
                return self._flash("No channels are switched on")
            self.status_var.set("Writing CSV…")
            threading.Thread(target=write, args=(data,), daemon=True).start()

        def write(data):
            try:
                n = min(len(t) for t, _ in data.values())
                t = next(iter(data.values()))[0][:n]
                columns = np.column_stack([t] + [v[:n] for _, v in data.values()])
                header = ",".join(["time_s"] + [f"{ch}_V" for ch in data])
                np.savetxt(path, columns, delimiter=",", header=header,
                           comments="", fmt="%.9g")
                msg = f"Saved {n} points × {len(data)} channel(s) to {path}"
            except (OSError, ValueError) as e:
                msg = f"CSV save failed: {e}"
            self.ui_queue.put((self._flash, (msg,)))

        self.jobs_submitted += 1
        self.worker.submit(fetch, done)

    # ---------- scope setups ----------
    def show_setups_menu(self):
        menu = self.setups_menu
        menu.delete(0, "end")
        saved = sorted(SETUP_DIR.glob("*.xml"), key=lambda p: p.stem.lower())
        for path in saved:
            menu.add_command(label=path.stem, command=lambda p=path: self.load_setup(p))
        if saved:
            menu.add_separator()
        menu.add_command(label="Save current setup…", command=self.save_setup)
        menu.add_command(label="Load from file…", command=self.load_setup_dialog)
        menu.add_command(label="Open setups folder", command=self.open_setups_folder)
        b = self.setups_btn
        menu.tk_popup(b.winfo_rootx(), b.winfo_rooty() + b.winfo_height())

    def save_setup(self):
        SETUP_DIR.mkdir(exist_ok=True)
        path = filedialog.asksaveasfilename(
            parent=self.root, title="Save scope setup — give it a name",
            defaultextension=".xml", initialdir=str(SETUP_DIR),
            filetypes=[("Scope setup", "*.xml")])
        if not path:
            return
        self.status_var.set("Reading setup from scope (takes a few seconds)…")

        def done(xml):
            if isinstance(xml, Exception):
                return self._flash(f"Could not read setup: {xml}")
            try:
                Path(path).write_bytes(xml)
                self._flash(f"Setup saved as “{Path(path).stem}”")
            except OSError as e:
                messagebox.showerror("Save failed", str(e), parent=self.root)

        self.jobs_submitted += 1
        self.worker.submit(lambda scope: scope.read_setup(), done)

    def load_setup_dialog(self):
        path = filedialog.askopenfilename(
            parent=self.root, title="Load scope setup",
            initialdir=str(SETUP_DIR if SETUP_DIR.exists() else APP_DIR),
            filetypes=[("Scope setup", "*.xml"), ("All files", "*.*")])
        if path:
            self.load_setup(Path(path))

    def load_setup(self, path):
        try:
            xml = path.read_bytes()
        except OSError as e:
            return messagebox.showerror("Load failed", str(e), parent=self.root)
        if b"<nsp_config" not in xml[:2000]:
            return messagebox.showerror(
                "Load failed", f"{path.name} is not a Siglent setup file.", parent=self.root)
        self.status_var.set(f"Restoring setup “{path.stem}”…")

        def done(result):
            self.worker.next_poll = 0.0
            if isinstance(result, Exception):
                self._flash(f"Could not restore setup: {result}")
            else:
                self._flash(f"Setup “{path.stem}” restored")

        self.jobs_submitted += 1
        self.worker.submit(lambda scope: scope.write_setup(xml), done)

    def open_setups_folder(self):
        SETUP_DIR.mkdir(exist_ok=True)
        os.startfile(SETUP_DIR)

    def _close(self):
        self.worker.stop()          # let it finish the request in flight,
        self.worker.join(2.0)       # then close the connection cleanly
        log("viewer closed")
        self.settings["geometry"] = self.root.geometry()
        self._save_settings()
        self.root.destroy()


def main():
    try:
        ctypes.windll.shcore.SetProcessDpiAwareness(1)   # crisp 1:1 pixels
    except (AttributeError, OSError):
        pass
    try:    # own taskbar identity, so Windows shows our icon rather than Python's
        ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID("SiglentViewer")
    except (AttributeError, OSError):
        pass
    log(f"viewer {__version__} started (process {os.getpid()})")
    root = tk.Tk()
    try:
        root.iconbitmap(default=str(APP_DIR / "siglent_viewer.ico"))
    except tk.TclError:
        pass
    app = App(root, sys.argv[1] if len(sys.argv) > 1 else None)
    root.update_idletasks()
    root.minsize(root.winfo_reqwidth() - 300, root.winfo_reqheight() - 150)
    geometry = app.settings.get("geometry")
    if geometry:
        try:
            root.geometry(geometry)
        except tk.TclError:
            pass
    dark_title_bar(root)
    root.mainloop()


if __name__ == "__main__":
    main()
