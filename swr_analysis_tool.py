"""
SWR Analysis Tool v1.0.0
Dark-theme Windows GUI for radio SWR sweeps using CAT.

Run from source:
    pip install pyserial matplotlib
    python swr_analysis_tool.py

Build EXE:
    pip install pyinstaller pyserial matplotlib
    pyinstaller --onefile --windowed --name SWR_Analysis_Tool swr_analysis_tool.py

Safety: This application keys the transmitter while scanning. Use low power,
a suitable antenna or dummy load, and remain present at the radio.
"""
from __future__ import annotations

import csv, json, queue, statistics, threading, time, subprocess, sys, math
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Optional, List
import tkinter as tk
from tkinter import ttk, messagebox, filedialog

try:
    import serial
    from serial.tools import list_ports
except Exception:
    serial = None
    list_ports = None

try:
    from matplotlib.figure import Figure
    from matplotlib.backends.backend_tkagg import FigureCanvasTkAgg
except Exception:
    Figure = None
    FigureCanvasTkAgg = None

APP_NAME = "SWR Analysis Tool"
APP_VERSION = "1.0.0"
BAUDS = ["4800", "9600", "19200", "38400", "57600", "115200"]
SETTINGS = Path.home() / ".swr_analysis_tool_settings.json"

C_BG = "#252525"; C_PANEL = "#303030"; C_FIELD = "#111111"; C_TEXT = "#e8e8e8"
C_MUTED = "#b8b8b8"; C_ACCENT = "#ffb000"; C_GLOW = "#8a5200"
# Very dim inactive segment color. Kept intentionally low so unused segments
# do not compete with active amber digits or make text look confusing.
C_DIM_AMBER = "#382404"; C_GRID = "#444444"; C_WARN = "#ffcc44"; C_BAD = "#ff5555"

class VoiceAnnouncer:
    """Windows-friendly queued speech helper.

    v0.3.0 intentionally prefers the Windows SAPI PowerShell path over a
    persistent pyttsx3 engine. In testing, persistent engines can sometimes
    hold onto or drop the final message at the end of a worker-thread scan.
    A serialized queue plus one synchronous SAPI call per message is slower,
    but much more reliable for short alerts like "Analysis completed".
    """
    def __init__(self):
        self._q = queue.Queue()
        threading.Thread(target=self._worker, daemon=True).start()

    def say(self, text: str):
        text = str(text).strip()
        if text:
            self._q.put(text)

    def _worker(self):
        while True:
            text = self._q.get()
            try:
                self._speak_now(text)
            finally:
                self._q.task_done()

    def _speak_now(self, text: str):
        if sys.platform.startswith('win'):
            ps_text = text.replace("'", "''")
            cmd = [
                'powershell', '-NoProfile', '-WindowStyle', 'Hidden', '-Command',
                "Add-Type -AssemblyName System.Speech; "
                "$s=New-Object System.Speech.Synthesis.SpeechSynthesizer; "
                "try {$s.SelectVoiceByHints([System.Speech.Synthesis.VoiceGender]::Female)} catch {}; "
                "$s.Rate=-1; $s.Volume=100; "
                f"$s.Speak('{ps_text}');"
            ]
            try:
                # Prevent the temporary PowerShell/SAPI speech process from
                # flashing a console window when the EXE is built with PyInstaller.
                startupinfo = None
                creationflags = 0
                if hasattr(subprocess, 'STARTUPINFO'):
                    startupinfo = subprocess.STARTUPINFO()
                    startupinfo.dwFlags |= subprocess.STARTF_USESHOWWINDOW
                    startupinfo.wShowWindow = 0
                if hasattr(subprocess, 'CREATE_NO_WINDOW'):
                    creationflags |= subprocess.CREATE_NO_WINDOW
                subprocess.run(
                    cmd,
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                    timeout=20,
                    startupinfo=startupinfo,
                    creationflags=creationflags,
                )
                return
            except Exception:
                pass
        # Optional fallback for non-Windows or locked-down PowerShell systems.
        try:
            import pyttsx3
            engine = pyttsx3.init()
            voices = engine.getProperty('voices') or []
            for v in voices:
                name = (getattr(v, 'name', '') or '').lower()
                if any(x in name for x in ['zira', 'female', 'hazel']):
                    engine.setProperty('voice', v.id)
                    break
            engine.setProperty('rate', 155)
            engine.say(text)
            engine.runAndWait()
            engine.stop()
        except Exception:
            pass

@dataclass
class ScanPoint:
    frequency_hz: int
    swr_raw: int
    swr_estimate: float
    response: str
    status: str

class FT710Cat:
    def __init__(self, port: str, baud: int, timeout: float = 0.75):
        if serial is None:
            raise RuntimeError("pyserial is not installed. Run: pip install pyserial")
        self.ser = serial.Serial(port=port, baudrate=baud, bytesize=serial.EIGHTBITS,
                                 parity=serial.PARITY_NONE, stopbits=serial.STOPBITS_ONE,
                                 timeout=timeout, write_timeout=timeout)
        time.sleep(0.2); self.flush()
    def close(self):
        try: self.ser.close()
        except Exception: pass
    def flush(self):
        try: self.ser.reset_input_buffer(); self.ser.reset_output_buffer()
        except Exception: pass
    def cmd(self, command: str, expect_prefix: Optional[str] = None, delay: float = 0.08) -> str:
        if not command.endswith(';'):
            command += ';'
        # Clear stale responses before each transaction. This is important when
        # Windows/USB buffering leaves an old RM6/FA reply in the CAT stream.
        try:
            self.ser.reset_input_buffer()
        except Exception:
            pass
        self.ser.write(command.encode('ascii'))
        self.ser.flush()
        time.sleep(delay)
        deadline = time.time() + max(float(self.ser.timeout or 0.75), 0.75)
        buf = bytearray()
        last_complete = ""
        while time.time() < deadline:
            b = self.ser.read(1)
            if not b:
                continue
            buf += b
            if b == b';':
                txt = buf.decode('ascii', errors='replace')
                last_complete = txt
                if expect_prefix is None or txt.startswith(expect_prefix):
                    return txt
                buf.clear()
        return last_complete or (buf.decode('ascii', errors='replace') if buf else "")
    def read_freq(self) -> str: return self.cmd("FA;", "FA")
    def read_mode(self) -> str: return self.cmd("MD0;", "MD")
    def read_power(self) -> str: return self.cmd("PC;", "PC")
    def read_tuner(self) -> str: return self.cmd("AC;", "AC")
    def set_mode_cwu(self): return self.cmd("MD03;", delay=0.08)
    def set_freq(self, hz: int): self.cmd(f"FA{hz:09d};", delay=0.05)
    def set_power(self, watts: int): self.cmd(f"PC{max(5,min(100,int(watts))):03d};", delay=0.05)
    def tx_on(self): self.cmd("TX1;", delay=0.08)
    def tx_off(self): self.cmd("TX0;", delay=0.08)
    def tuner_enable(self): return self.cmd("AC001;", delay=0.1)
    def tune(self): return self.cmd("AC002;", delay=0.1)
    def read_ri0(self) -> str: return self.cmd("RI0;", "RI0", delay=0.06)
    def read_swr_raw(self) -> tuple[Optional[int], str]:
        resp = self.cmd("RM6;", "RM6", delay=0.06)
        try: return int(resp[3:6]), resp
        except Exception: return None, resp

def raw_to_swr(raw: int) -> float:
    # The raw value comes directly from the radio CAT SWR meter command RM6;.
    # The radio returns RM6xxx000; with xxx from 000-255. The display below
    # converts that radio-provided raw meter number to a human SWR estimate.
    raw = max(0, min(255, int(raw)))
    return max(1.0, math.floor((1.0 + (raw / 255.0) * 8.9) * 10) / 10)

def parse_mhz(s: str) -> int:
    """Parse a frequency entry in MHz.

    Accepts either decimal point or decimal comma, so both 14.000 and
    14,000 are interpreted as 14.000 MHz.  Commas are treated as decimal
    separators rather than thousands separators because the entry fields are
    labeled in MHz and ham operators often type band frequencies that way.
    """
    text = (s or "").strip()
    if not text:
        raise ValueError("Please enter a frequency in MHz.")
    text = text.replace(",", ".")
    try:
        mhz = float(text)
    except ValueError:
        raise ValueError(f"Invalid frequency '{s}'. Please enter a numeric value in MHz, such as 14.000.")
    if mhz <= 0:
        raise ValueError("Frequency must be greater than 0 MHz.")
    return int(round(mhz * 1_000_000))

# Conservative transmit-range guard for the current supported radio profile.
# This prevents accidental attempts to sweep clearly invalid frequencies such
# as 144.000 MHz on an FT-710-class HF/50 MHz transceiver. 60m is omitted
# because it is channelized and not appropriate for automated sweeps.
ALLOWED_SWEEP_BANDS = [
    (1_800_000, 2_000_000, "160m"),
    (3_500_000, 4_000_000, "80m"),
    (7_000_000, 7_300_000, "40m"),
    (10_100_000, 10_150_000, "30m"),
    (14_000_000, 14_350_000, "20m"),
    (18_068_000, 18_168_000, "17m"),
    (21_000_000, 21_450_000, "15m"),
    (24_890_000, 24_990_000, "12m"),
    (28_000_000, 29_700_000, "10m"),
    (50_000_000, 54_000_000, "6m"),
]

def sweep_band_for_range(start_hz: int, stop_hz: int):
    lo, hi = sorted((start_hz, stop_hz))
    for b_lo, b_hi, name in ALLOWED_SWEEP_BANDS:
        if lo >= b_lo and hi <= b_hi:
            return name
    return None

def allowed_bands_text() -> str:
    return ", ".join([f"{name} {lo/1_000_000:.3f}-{hi/1_000_000:.3f} MHz" for lo, hi, name in ALLOWED_SWEEP_BANDS])

def validate_sweep_inputs(start_hz: int, stop_hz: int, step_hz: int):
    if step_hz <= 0:
        raise ValueError("Step size must be greater than 0 kHz.")
    if start_hz == stop_hz:
        raise ValueError("Start and stop frequencies must not be identical.")
    band = sweep_band_for_range(start_hz, stop_hz)
    if not band:
        raise ValueError(
            "Sweep range is outside the supported transmit bands or crosses a band edge.\n\n"
            "For safety, the entire sweep must stay inside one supported amateur band.\n\n"
            f"Allowed ranges: {allowed_bands_text()}"
        )
    return band

def fmt_freq(hz: int) -> str:
    mhz = hz // 1_000_000; rem = hz % 1_000_000
    return f"{mhz}.{rem//1000:03d}.{rem%1000:03d}"
def mhz_float(hz: int) -> float: return hz / 1_000_000.0

class LedDisplay(tk.Canvas):
    """Amber instrument-style frequency display with LCD-like annunciators.

    The large segmented area is now reserved for frequency only. CAT state is
    shown in a small annunciator strip inside the same display bezel:
        CAT DATA OK   NO CAT DATA   SCANNING   TX
    Active annunciators are bright amber; inactive ones are dim amber.
    """
    SEGMENTS = {
        "0": "abcdef", "1": "bc", "2": "abged", "3": "abgcd",
        "4": "fgbc", "5": "afgcd", "6": "afgecd", "7": "abc",
        "8": "abcdefg", "9": "abfgcd", "-": "g", " ": "",
    }
    def __init__(self, master, text="", **kw):
        super().__init__(master, height=100, bg="#120b02", highlightthickness=1,
                         highlightbackground="#050505", **kw)
        self.text = text
        self.cat_ok = False
        self.no_cat = True
        self.scanning = False
        self.tx = False
        # Redraw whenever the canvas is resized. This keeps the unlit
        # startup/CAT-lost frequency field centered the same way as real
        # frequency values after CAT data arrives.
        self.bind("<Configure>", lambda _e: self.draw())
        self.after(50, self.draw)
        self.after_idle(self.draw)

    def set(self, text: str):
        """Compatibility wrapper: only numeric frequency-like text is displayed."""
        text = (text or "").strip()
        if any(ch.isdigit() for ch in text):
            self.text = text
        else:
            self.text = ""
        self.draw()

    def set_frequency(self, text: str | None):
        self.text = (text or "").strip()
        self.draw()

    def set_indicators(self, *, cat_ok=None, no_cat=None, scanning=None, tx=None):
        if cat_ok is not None: self.cat_ok = bool(cat_ok)
        if no_cat is not None: self.no_cat = bool(no_cat)
        if scanning is not None: self.scanning = bool(scanning)
        if tx is not None: self.tx = bool(tx)
        self.draw()

    def _segment(self, x, y, w, h, name, color):
        t = max(4, int(w * 0.13))
        if name == "a": pts = [x+t,y, x+w-t,y, x+w-2*t,y+t, x+2*t,y+t]
        elif name == "g": pts = [x+2*t,y+h//2-t//2, x+w-2*t,y+h//2-t//2, x+w-t,y+h//2, x+w-2*t,y+h//2+t//2, x+2*t,y+h//2+t//2, x+t,y+h//2]
        elif name == "d": pts = [x+2*t,y+h-t, x+w-2*t,y+h-t, x+w-t,y+h, x+t,y+h]
        elif name == "f": pts = [x,y+t, x+t,y+2*t, x+t,y+h//2-t, x,y+h//2]
        elif name == "b": pts = [x+w-t,y+2*t, x+w,y+t, x+w,y+h//2, x+w-t,y+h//2-t]
        elif name == "e": pts = [x,y+h//2, x+t,y+h//2+t, x+t,y+h-2*t, x,y+h-t]
        elif name == "c": pts = [x+w-t,y+h//2+t, x+w,y+h//2, x+w,y+h-t, x+w-t,y+h-2*t]
        else: return
        self.create_polygon(pts, fill=color, outline="")

    def _draw_char(self, ch, x, y, w, h):
        ch = ch.upper()
        # Draw inactive segment guides first, like a powered but unlit LCD.
        for seg in "abcdefg":
            self._segment(x, y, w, h, seg, C_DIM_AMBER)
        for seg in self.SEGMENTS.get(ch, ""):
            # Light amber glow restored, but not strong enough to confuse unused segments.
            self._segment(x-2, y-2, w+4, h+4, seg, "#4a2c00")
            self._segment(x-1, y-1, w+2, h+2, seg, "#8a5200")
            self._segment(x, y, w, h, seg, C_ACCENT)

    def _measure(self, chars, digit_w, dot_w, gap):
        total = 0
        for ch in chars:
            total += dot_w if ch == "." else digit_w
            total += gap
        return max(0, total - gap)

    def _draw_annunciator(self, x, y, label, active):
        color = C_ACCENT if active else "#4b3106"
        glow = "#6a4300" if active else "#241704"
        # Tiny glow shadow for active labels.
        if active:
            self.create_text(x+1, y+1, text=label, anchor="w", fill=glow, font=("Arial", 8, "bold"))
        self.create_text(x, y, text=label, anchor="w", fill=color, font=("Arial", 8, "bold"))

    def draw(self):
        self.delete("all")
        w = max(self.winfo_width(), 100); h = max(self.winfo_height(), 100)
        # Dark amber stippled background.
        for x in range(8, w-8, 7):
            for y in range(8, h-8, 7):
                self.create_rectangle(x, y, x+2, y+2, fill="#251701", outline="")

        text = (self.text or "").strip().upper()
        # The display always reserves a fixed HF-frequency field, measured as
        # 88.888.888. That prevents the startup unlit field from sitting left
        # and prevents the readout from jumping when CAT data arrives.
        template = "88.888.888"
        if not text or not any(ch.isdigit() for ch in text):
            text = template
            active_digits = False
        else:
            active_digits = True
        chars = [ch if (ch.isdigit() or ch == ".") else " " for ch in text]

        # Fixed, bolder instrument proportions for frequencies.
        ann_h = 18
        digit_h = max(56, min(68, h - ann_h - 18))
        digit_w = max(33, int(digit_h * 0.68))
        dot_w = max(8, int(digit_w * 0.25))
        gap = max(4, int(digit_w * 0.13))
        template_total = self._measure(list(template), digit_w, dot_w, gap)
        available = max(80, w - 28)
        if template_total > available:
            scale_x = available / template_total
            digit_w = max(26, int(digit_w * scale_x))
            dot_w = max(6, int(dot_w * scale_x))
            gap = max(3, int(gap * scale_x))
            template_total = self._measure(list(template), digit_w, dot_w, gap)

        # Center based on the template width, not on the currently drawn value.
        x = max(10, (w - template_total) // 2)
        y = max(6, (h - ann_h - digit_h) // 2)
        for ch in chars:
            if ch == ".":
                r = max(3, dot_w//2)
                if active_digits:
                    self.create_oval(x+1, y+digit_h-r-2, x+1+r*2, y+digit_h-2, fill="#4a2c00", outline="")
                    self.create_oval(x+2, y+digit_h-r-3, x+2+r*2-2, y+digit_h-3, fill=C_ACCENT, outline="")
                else:
                    self.create_oval(x+2, y+digit_h-r-3, x+2+r*2-2, y+digit_h-3, fill=C_DIM_AMBER, outline="")
                x += dot_w + gap
            else:
                if active_digits:
                    self._draw_char(ch, x, y, digit_w, digit_h)
                else:
                    # Draw all guides only, no active segments.
                    for seg in "abcdefg":
                        self._segment(x, y, digit_w, digit_h, seg, C_DIM_AMBER)
                x += digit_w + gap

        # Annunciator strip inside the same display bezel.
        ay = h - 16
        labels = [("CAT DATA OK", self.cat_ok), ("NO CAT DATA", self.no_cat), ("SCANNING", self.scanning), ("TX", self.tx)]
        widths = [92, 92, 78, 24]
        total_labels = sum(widths) + 16 * (len(widths) - 1)
        ax = max(10, (w - total_labels) // 2)
        for (label, active), lw in zip(labels, widths):
            self._draw_annunciator(ax, ay, label, active)
            ax += lw + 16

class MiniAppIcon(tk.Canvas):
    """Small built-in app mark for the header.

    Drawn directly in Tk so the visible header icon always matches the amber
    instrument theme, even before Windows finishes refreshing the EXE icon cache.
    """
    def __init__(self, master, **kw):
        super().__init__(master, **kw)
        self.bind("<Configure>", lambda _e: self.draw())
        self.after(50, self.draw)

    def draw(self):
        self.delete("all")
        w = max(24, self.winfo_width())
        h = max(24, self.winfo_height())
        self.create_rectangle(2, 2, w-2, h-2, fill="#111111", outline="#5a5a5a")
        self.create_rectangle(5, 5, w-5, 11, fill="#2a1a02", outline="")
        self.create_text(w/2, 8, text="710", fill=C_ACCENT, font=("Consolas", 6, "bold"))
        pts = [(5, h-6), (9, h-12), (13, h-9), (17, h-16), (w-5, h-8)]
        self.create_line(*sum(pts, ()), fill=C_ACCENT, width=2, smooth=True)
        self.create_line(5, h-6, w-5, h-6, fill="#6d4400", width=1)

class App(tk.Tk):
    def __init__(self):
        super().__init__(); self.title(f"{APP_NAME} - v{APP_VERSION}")
        self.geometry("1120x640"); self.minsize(900, 520); self.configure(bg=C_BG)
        self._set_app_icon()
        self.after(100, self._enable_dark_title_bar)
        self.voice = VoiceAnnouncer()
        self.results: List[ScanPoint] = []; self.stop_event = threading.Event(); self.q = queue.Queue(); self.scan_thread = None
        self.cat_connected = False
        self._style(); self._vars(); self._build(); self._load_settings(); self._refresh_ports(); self._install_setting_traces(); self.after(100, self._pump); self.after(3000, self._cat_watchdog); self.protocol("WM_DELETE_WINDOW", self._on_close)
    def _resource_path(self, name: str) -> Path:
        """Return a path that works from source or a PyInstaller bundle."""
        base = Path(getattr(sys, "_MEIPASS", Path(__file__).resolve().parent))
        return base / name

    def _set_app_icon(self):
        """Use the same icon for the title bar, taskbar, Alt-Tab, and EXE."""
        ico = self._resource_path("swr_analysis_tool.ico")
        try:
            if ico.exists():
                self.iconbitmap(default=str(ico))
        except Exception:
            pass

    def _enable_dark_title_bar(self):
        """Ask Windows 10/11 to use an immersive dark title bar where supported."""
        if not sys.platform.startswith("win"):
            return
        try:
            import ctypes
            self.update_idletasks()
            hwnd = ctypes.windll.user32.GetParent(self.winfo_id()) or self.winfo_id()
            value = ctypes.c_int(1)
            # DWMWA_USE_IMMERSIVE_DARK_MODE. Attribute 20 is current;
            # attribute 19 works on some older Windows 10 builds.
            for attr in (20, 19):
                try:
                    ctypes.windll.dwmapi.DwmSetWindowAttribute(
                        ctypes.c_void_p(hwnd),
                        ctypes.c_int(attr),
                        ctypes.byref(value),
                        ctypes.sizeof(value),
                    )
                except Exception:
                    pass
        except Exception:
            pass

    def _style(self):
        st = ttk.Style(self); st.theme_use('clam')
        st.configure('.', background=C_BG, foreground=C_TEXT, fieldbackground=C_FIELD, bordercolor=C_GRID, lightcolor=C_GRID, darkcolor=C_GRID)
        st.configure('TFrame', background=C_BG); st.configure('Panel.TFrame', background=C_PANEL)
        st.configure('TLabel', background=C_BG, foreground=C_TEXT); st.configure('TLabelframe', background=C_BG, foreground=C_TEXT); st.configure('TLabelframe.Label', background=C_BG, foreground=C_TEXT)
        st.configure('TButton', background="#555555", foreground="#ffffff", padding=6); st.map('TButton', background=[('active','#6a6a6a')])
        st.configure('Good.TButton', background="#1f7a3a", foreground="#ffffff", padding=6)
        st.map('Good.TButton', background=[('active', '#2e9d4f'), ('disabled', '#444444')])
        st.configure('Bad.TButton', background="#8b1f1f", foreground="#ffffff", padding=6)
        st.map('Bad.TButton', background=[('active', '#b02a2a'), ('disabled', '#444444')])
        st.configure('TEntry', fieldbackground=C_FIELD, foreground=C_TEXT, insertcolor=C_TEXT)
        st.configure('TCombobox', fieldbackground=C_FIELD, background=C_FIELD, foreground=C_TEXT, arrowcolor=C_TEXT, selectbackground='#0078d7', selectforeground='white')
        st.map('TCombobox', fieldbackground=[('readonly', C_FIELD)], foreground=[('readonly', C_TEXT)], selectbackground=[('readonly', '#0078d7')], selectforeground=[('readonly', 'white')])
        self.option_add('*TCombobox*Listbox.background', C_FIELD)
        self.option_add('*TCombobox*Listbox.foreground', C_TEXT)
        self.option_add('*TCombobox*Listbox.selectBackground', '#0078d7')
        self.option_add('*TCombobox*Listbox.selectForeground', 'white')
        st.configure('Treeview', background="#141414", foreground=C_TEXT, fieldbackground="#141414", rowheight=24)
        st.configure('Treeview.Heading', background="#3b3b3b", foreground=C_TEXT)
        # Dark scrollbar styling for sweep results. This avoids the bright
        # default Windows/ttk scrollbar that visually clashes with the CRT
        # amber/dark instrument theme.
        st.configure(
            'Dark.Vertical.TScrollbar',
            background='#3a3a3a',
            troughcolor='#141414',
            bordercolor='#141414',
            arrowcolor=C_ACCENT,
            lightcolor='#3a3a3a',
            darkcolor='#202020',
            relief='flat',
            width=14,
        )
        st.map(
            'Dark.Vertical.TScrollbar',
            background=[('active', '#4a4a4a'), ('pressed', '#5a5a5a')],
            arrowcolor=[('active', C_ACCENT), ('pressed', C_ACCENT)],
        )
        st.configure('TNotebook', background=C_BG); st.configure('TNotebook.Tab', background="#333", foreground=C_TEXT, padding=(10,4)); st.map('TNotebook.Tab', background=[('selected','#444')])
    def _vars(self):
        self.port_var=tk.StringVar(value="COM4"); self.baud_var=tk.StringVar(value="115200")
        self.start_var=tk.StringVar(value="14.000"); self.stop_var=tk.StringVar(value="14.350"); self.step_var=tk.StringVar(value="5")
        self.power_var=tk.StringVar(value="5"); self.settle_var=tk.StringVar(value="0.50"); self.samples_var=tk.StringVar(value="3"); self.max_swr_var=tk.StringVar(value="3.0")
        self.status_var=tk.StringVar(value="CAT: NO DATA"); self.points_var=tk.StringVar(value="0 points"); self.scan_var=tk.StringVar(value="Waiting for CAT")
        self.best_var=tk.StringVar(value="Best: --"); self.swr_var=tk.StringVar(value="SWR --"); self.raw_var=tk.StringVar(value="RAW --")
        self.csv_path_var=tk.StringVar(value=str(Path.home()/"Documents"/"swr_scan.csv"))
        self.suppress_safety_var=tk.BooleanVar(value=False)
    def _build(self):
        self._build_menu()
        root_frame = tk.Frame(self, bg=C_BG)
        root_frame.pack(fill=tk.BOTH, expand=True, padx=10, pady=(8, 4))

        top=tk.Frame(root_frame,bg=C_BG)
        top.pack(fill=tk.X, pady=(0,8))
        # Use grid so the display can resize cleanly without hiding controls.
        # The application title is centered inside the GUI, while the icon
        # remains only in the Windows title bar/taskbar.
        tk.Label(
            top, text=f"{APP_NAME}   v{APP_VERSION}",
            bg=C_BG, fg=C_ACCENT, font=("Segoe UI", 11, "bold")
        ).grid(row=0, column=0, columnspan=3, sticky="ew", pady=(0,5))
        self.led=LedDisplay(top,width=560)
        self.led.grid(row=1,column=1,sticky="ew",padx=(8,12))
        self.cat_line=tk.StringVar(value="Radio   Port: --   Baud: --   Status: NO CAT DATA")
        tk.Label(top,textvariable=self.cat_line,bg=C_BG,fg=C_MUTED,font=("Segoe UI",8)).grid(row=2,column=1,sticky="ew",padx=(8,12),pady=(2,0))
        buttons=tk.Frame(top,bg=C_BG)
        buttons.grid(row=1,column=2,sticky="e")
        top.columnconfigure(0,weight=1,minsize=120)
        top.columnconfigure(1,weight=3,minsize=480)
        top.columnconfigure(2,weight=1,minsize=120)

        main = ttk.Frame(root_frame)
        main.pack(fill=tk.BOTH, expand=True)
        main.columnconfigure(0, weight=0)
        main.columnconfigure(1, weight=3)
        main.columnconfigure(2, weight=2)
        main.rowconfigure(0, weight=1)

        self.control_panel = ttk.Frame(main)
        self.control_panel.grid(row=0, column=0, sticky='ns', padx=(0,8), pady=0)
        self.graph_panel = ttk.Frame(main)
        self.graph_panel.grid(row=0, column=1, sticky='nsew', padx=(0,8), pady=0)
        self.results_panel = ttk.Frame(main)
        self.results_panel.grid(row=0, column=2, sticky='nsew', pady=0)

        self._build_control_panel(self.control_panel)
        self._build_graph_panel(self.graph_panel)
        self._build_results_panel(self.results_panel)


    def _build_menu(self):
        """Dark in-window menu bar.

        Tk's native Windows menu bar is controlled mostly by the OS and often
        remains light even when the title bar is dark. This custom menu bar
        keeps the program visually consistent while preserving normal Setup /
        Export / Help menu behavior.
        """
        bar = tk.Frame(self, bg="#1a1a1a", highlightthickness=0)
        bar.pack(fill=tk.X, side=tk.TOP)
        self.custom_menu_bar = bar

        def make_menu_button(label):
            btn = tk.Menubutton(
                bar, text=label, bg="#1a1a1a", fg=C_TEXT,
                activebackground="#333333", activeforeground=C_ACCENT,
                relief=tk.FLAT, padx=12, pady=4, font=("Segoe UI", 9)
            )
            menu = tk.Menu(
                btn, tearoff=0, bg="#1f1f1f", fg=C_TEXT,
                activebackground="#3a3a3a", activeforeground=C_ACCENT,
                disabledforeground="#777777", borderwidth=0, relief=tk.FLAT
            )
            btn.configure(menu=menu)
            btn.pack(side=tk.LEFT)
            return btn, menu

        _, setup_menu = make_menu_button("Setup")
        setup_menu.add_command(label="Setup", command=self._open_setup_dialog)
        setup_menu.add_separator()
        setup_menu.add_command(label="Exit", command=self._on_close)

        _, export_menu = make_menu_button("Export")
        export_menu.add_command(label="Export CSV", command=self._save_csv)
        export_menu.add_command(label="Export Image", command=self._save_png)

        _, help_menu = make_menu_button("Help")
        help_menu.add_command(label="About", command=self._show_about)

    def _build_control_panel(self, parent):
        def row(r,label,var):
            ttk.Label(parent,text=label).grid(row=r,column=0,sticky='w',pady=4)
            ttk.Entry(parent,textvariable=var,width=12).grid(row=r,column=1,padx=6,pady=4)
        for r,args in enumerate([("Start MHz",self.start_var),("Stop MHz",self.stop_var),("Step kHz",self.step_var),("Power W",self.power_var),("Dwell sec",self.settle_var),("Samples",self.samples_var),("Abort SWR",self.max_swr_var)]):
            row(r,*args)
        self.connect_btn=ttk.Button(parent,text="CONNECT / TEST CAT",command=self._test_cat)
        self.connect_btn.grid(row=7,column=0,columnspan=2,sticky='ew',pady=(12,4))
        self.scan_btn=ttk.Button(parent,text="START SWEEP",command=self._start_scan,state=tk.DISABLED)
        self.scan_btn.grid(row=8,column=0,columnspan=2,sticky='ew',pady=4)
        self.stop_btn=ttk.Button(parent,text="STOP",command=self._stop,state=tk.DISABLED)
        self.stop_btn.grid(row=9,column=0,columnspan=2,sticky='ew',pady=4)
        ttk.Separator(parent).grid(row=10,column=0,columnspan=2,sticky='ew',pady=10)
        ttk.Label(parent,textvariable=self.swr_var,font=("Segoe UI",18,"bold")).grid(row=11,column=0,columnspan=2)
        ttk.Label(parent,textvariable=self.raw_var,font=("Segoe UI",12)).grid(row=12,column=0,columnspan=2)

    def _build_graph_panel(self, parent):
        parent.rowconfigure(0, weight=1)
        parent.columnconfigure(0, weight=1)
        if Figure:
            self.fig=Figure(figsize=(5.8,3.6),dpi=100,facecolor=C_BG)
            self.ax=self.fig.add_subplot(111)
            self._style_crt_axes()
            self.canvas=FigureCanvasTkAgg(self.fig,master=parent)
            self.canvas.get_tk_widget().grid(row=0,column=0,sticky='nsew')
        else:
            self.canvas=None; ttk.Label(parent,text="matplotlib is required for graphing").grid(row=0,column=0)

    def _graph_upper_limit(self) -> float:
        try:
            value = float(self.max_swr_var.get().replace(',', '.'))
        except Exception:
            value = 3.0
        # SWR cannot be below 1.0. Keep a little headroom if the user enters
        # something too low, but otherwise let the protection threshold define
        # the top of the CRT display.
        return max(1.1, min(10.0, value))

    def _style_crt_axes(self):
        """Apply the amber CRT analyzer look to the SWR graph only."""
        self.ax.set_facecolor('#070502')
        self.fig.patch.set_facecolor(C_BG)
        self.ax.set_title("SWR Analysis Sweep", color=C_ACCENT)
        self.ax.set_xlabel("Frequency MHz", color=C_ACCENT)
        self.ax.set_ylabel("SWR", color=C_ACCENT)
        self.ax.tick_params(axis='both', which='major', colors=C_ACCENT, labelsize=8, length=4)
        self.ax.tick_params(axis='y', which='minor', colors='#6a4300', length=2)
        for spine in self.ax.spines.values():
            spine.set_color('#8a5200')

        # Ham-radio SWR scale: never start at 0.0. The top of the display is
        # tied to the user's high-SWR protection threshold, so if Abort SWR is
        # 4.0 the graph spans 1.0 through 4.0 instead of wasting space to 10.0.
        ymax = self._graph_upper_limit()
        self.ax.set_ylim(1.0, ymax)

        # Minor grid: 0.1 SWR detail from 1.0 up to the visible top, with the
        # fine detail capped at 4.0 to avoid a crowded CRT display.
        minor_top = min(ymax, 4.0)
        minor_ticks = []
        v = 1.0
        while v <= minor_top + 1e-9:
            minor_ticks.append(round(v, 1))
            v += 0.1

        # Major labels: granular enough for antenna tuning, but not so dense
        # that the left axis becomes unreadable on laptop screens.
        major_ticks = []
        v = 1.0
        while v <= min(ymax, 3.0) + 1e-9:
            # label every 0.5, keeping the 0.1 grid lines as minor ticks
            if abs((v * 10) % 5) < 1e-6:
                major_ticks.append(round(v, 1))
            v += 0.1
        if ymax > 3.0:
            n = 4
            while n <= int(math.floor(ymax)):
                major_ticks.append(float(n))
                n += 1
        if round(ymax, 1) not in [round(x, 1) for x in major_ticks]:
            major_ticks.append(round(ymax, 1))
        major_ticks = sorted(set(major_ticks))

        self.ax.set_yticks(major_ticks)
        self.ax.set_yticklabels([f"{v:.1f}" for v in major_ticks])
        self.ax.set_yticks(minor_ticks, minor=True)
        self.ax.grid(True, which='major', color='#8a5200', alpha=0.32, linewidth=0.75)
        self.ax.grid(True, which='minor', color='#8a5200', alpha=0.12, linewidth=0.35)
        # Subtle CRT scanline feel inside the plot area. Kept light so it does
        # not compete with the data trace or make the graph hard to read.
        y = 1.0
        step = max(0.1, (ymax - 1.0) / 36.0)
        while y <= ymax + 1e-9:
            self.ax.axhline(y, color='#2c1b00', linewidth=0.25, alpha=0.16, zorder=0)
            y += step

    def _build_results_panel(self, parent):
        parent.rowconfigure(0, weight=1)
        parent.columnconfigure(0, weight=1)
        cols=("freq","raw","swr","status")
        self.tree=ttk.Treeview(parent,columns=cols,show='headings')
        heads=[("freq","Frequency"),("raw","Raw"),("swr","SWR"),("status","Status")]
        widths={"freq":110,"raw":60,"swr":70,"status":210}
        for c,h in heads:
            self.tree.heading(c,text=h)
            self.tree.column(c,width=widths[c], stretch=(c=="status"))
        vsb=ttk.Scrollbar(parent, orient="vertical", command=self.tree.yview, style="Dark.Vertical.TScrollbar")
        self.tree.configure(yscrollcommand=vsb.set)
        self.tree.grid(row=0,column=0,sticky='nsew')
        vsb.grid(row=0,column=1,sticky='ns')

    def _open_setup_dialog(self):
        dlg = tk.Toplevel(self)
        dlg.title("Setup")
        dlg.configure(bg=C_BG)
        dlg.transient(self)
        dlg.grab_set()
        dlg.resizable(True, True)
        dlg.geometry("720x500")
        try:
            ico = self._resource_path("swr_analysis_tool.ico")
            if ico.exists(): dlg.iconbitmap(default=str(ico))
        except Exception:
            pass
        f=ttk.Frame(dlg)
        f.pack(fill=tk.BOTH,expand=True,padx=12,pady=12)
        ttk.Label(f,text="Port:").grid(row=0,column=0,sticky='w',pady=6)
        port_combo=ttk.Combobox(f,textvariable=self.port_var,width=50)
        port_combo.grid(row=0,column=1,sticky='ew',pady=6)
        self.port_combo = port_combo
        ttk.Button(f,text="Refresh",command=self._refresh_ports).grid(row=0,column=2,padx=6)
        ttk.Label(f,text="Baud:").grid(row=1,column=0,sticky='w',pady=6)
        ttk.Combobox(f,textvariable=self.baud_var,values=BAUDS,width=12,state='readonly').grid(row=1,column=1,sticky='w',pady=6)
        ttk.Button(f,text="RST COM / Test",command=self._test_cat).grid(row=1,column=2,padx=6)
        ttk.Label(f,text="Freeform CAT:").grid(row=2,column=0,sticky='w',pady=(18,6))
        self.free_var=tk.StringVar(value=getattr(self, 'free_var', tk.StringVar(value='FA;')).get() if hasattr(self, 'free_var') else "FA;")
        ttk.Entry(f,textvariable=self.free_var,width=70).grid(row=2,column=1,sticky='ew',pady=(18,6))
        ttk.Button(f,text="Send",command=self._send_freeform).grid(row=2,column=2,padx=6,pady=(18,6))
        self.log=tk.Text(f,bg="#111",fg="white",insertbackground="white",height=16,wrap="none")
        self.log.grid(row=3,column=0,columnspan=3,sticky='nsew',pady=8)
        ttk.Button(f,text="Close",command=dlg.destroy).grid(row=4,column=2,sticky='e',pady=(8,0))
        f.rowconfigure(3,weight=1); f.columnconfigure(1,weight=1)
        self._refresh_ports()

    def _show_about(self):
        text = (
            f"{APP_NAME}\n"
            f"Version: {APP_VERSION}\n\n"
            "This program is designed to help amateur radio operators perform controlled SWR analysis sweeps over a user-defined frequency range. "
            "It uses a radio CAT interface to set frequency, key the transmitter briefly at low power, read SWR meter data, plot the sweep, and export results.\n\n"
            "The tool automatically switches the radio to CW-U for the analysis so RF can be generated without injecting audio tones. "
            "It also includes high-SWR protection, CSV export, image export, CAT monitoring, and a radio-style amber instrument display.\n\n"
            "Envisioned by N4EAC, Eduardo A. de Carvalho.\n"
            "Coded with AI."
        )
        messagebox.showinfo(f"About {APP_NAME}", text)

    def _cat(self):
        port=self.port_var.get().split()[0]
        return FT710Cat(port,int(self.baud_var.get()))

    def _mark_cat_ok(self, freq_response: str = ""):
        self.cat_connected = True
        self.scan_btn.config(state=tk.NORMAL)
        if hasattr(self, 'connect_btn'):
            self.connect_btn.config(state=tk.DISABLED)
        self.status_var.set("CAT: CONNECTED")
        self.cat_line.set(f"Radio   Port: {self.port_var.get().split()[0]}   Baud: {self.baud_var.get()}   Status: CONNECTED")
        self.led.set_indicators(cat_ok=True, no_cat=False, scanning=False, tx=False)
        if freq_response.startswith('FA') and len(freq_response) >= 12:
            try:
                self.led.set_frequency(fmt_freq(int(freq_response[2:11])))
            except Exception:
                pass

    def _mark_cat_lost(self, note: str = "CAT LOST"):
        self.cat_connected = False
        self.scan_btn.config(state=tk.DISABLED)
        if hasattr(self, 'connect_btn'):
            self.connect_btn.config(state=tk.NORMAL)
        self.status_var.set("CAT: NO DATA")
        self.scan_var.set(note)
        self.led.set_frequency(None)
        self.led.set_indicators(cat_ok=False, no_cat=True, scanning=False, tx=False)
        self.cat_line.set(f"Radio   Port: {self.port_var.get().split()[0] if self.port_var.get() else '--'}   Baud: {self.baud_var.get()}   Status: CAT LOST")
    def _refresh_ports(self):
        vals=[]
        if list_ports:
            for p in list_ports.comports():
                vals.append(f"{p.device}  {p.description}")
        
        if hasattr(self, 'port_combo') and self.port_combo is not None:
            self.port_combo['values']=vals if vals else [self.port_var.get()]
        # prefer enhanced / COM4, but do not override user choice if present
        for v in vals:
            if "Enhanced" in v or v.startswith("COM4"):
                if not self.port_var.get(): self.port_var.set(v)
                break
    def _log(self,msg):
        try:
            if hasattr(self, "log") and self.log.winfo_exists():
                self.log.insert(tk.END,datetime.now().strftime("%H:%M:%S ")+msg+"\n"); self.log.see(tk.END)
        except Exception:
            pass
    def _test_cat(self):
        if self.cat_connected:
            return
        if hasattr(self, 'connect_btn'):
            self.connect_btn.config(state=tk.DISABLED)
        self.led.set_frequency(None)
        self.led.set_indicators(cat_ok=False, no_cat=True, scanning=False, tx=False)
        self.scan_var.set("Connecting to radio...")
        self.cat_line.set(f"Radio   Port: {self.port_var.get().split()[0]}   Baud: {self.baud_var.get()}   Status: CONNECTING")
        self.update_idletasks()
        try:
            cat = self._cat()
            fa = cat.read_freq()
            if not (fa.startswith('FA') and len(fa) >= 12):
                raise RuntimeError(f"No valid FA frequency response from radio. Response: {fa or '<none>'}")
            pc = cat.read_power()
            md_before = cat.read_mode()
            mode_resp = cat.set_mode_cwu()
            md = cat.read_mode()
            cat.close()
            self._mark_cat_ok(fa)
            self.scan_var.set(f"CAT OK {fa} {pc} {md}")
            self._log(f"CAT OK: {fa} {pc} Mode before={md_before} CW-U set={mode_resp} Mode now={md}")
        except Exception as e:
            try:
                cat.close()
            except Exception:
                pass
            self._mark_cat_lost("CAT failed")
            messagebox.showerror("CAT Test Failed", str(e))

    def _send_freeform(self):
        try:
            cat=self._cat(); resp=cat.cmd(self.free_var.get()); cat.close(); self._log(f"> {self.free_var.get()}  < {resp}")
        except Exception as e: self._log(f"ERROR {e}")
    def _manual_tx(self):
        if not self.cat_connected:
            if hasattr(self, 'ptt_btn'):
                self.ptt_btn.config(state=tk.DISABLED, style='TButton')
            return
        if not messagebox.askyesno("PTT Test", "This will key the radio via CAT for a short test. Continue?"):
            return
        cat = None
        try:
            cat=self._cat()
            cat.tx_on()
            time.sleep(.5)
            cat.tx_off()
            cat.close()
            self._log("PTT test pulse complete")
            if hasattr(self, 'ptt_btn'):
                self.ptt_btn.config(style='Good.TButton')
        except Exception as e:
            try:
                if cat:
                    cat.tx_off(); cat.close()
            except Exception:
                pass
            if hasattr(self, 'ptt_btn'):
                self.ptt_btn.config(style='Bad.TButton')
            self._mark_cat_lost("CAT LOST")
            messagebox.showerror("PTT Test Failed",str(e))
    def _manual_tnr(self):
        try: cat=self._cat(); self._log("TNR EN: "+cat.tuner_enable()); cat.close()
        except Exception as e: messagebox.showerror("Tuner Failed",str(e))
    def _manual_tune(self):
        if not messagebox.askyesno("Tune", "This may transmit while the radio tuner runs. Continue?"): return
        try: cat=self._cat(); self._log("TUNE: "+cat.tune()); cat.close()
        except Exception as e: messagebox.showerror("Tune Failed",str(e))
    def _default_export_name(self, ext: str) -> str:
        stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        return f"swr_analysis_{stamp}.{ext.lstrip('.')}"

    def _show_safety_dialog(self) -> bool:
        dlg = tk.Toplevel(self)
        dlg.title("Safety Check")
        dlg.configure(bg=C_BG)
        dlg.transient(self)
        dlg.grab_set()
        dlg.resizable(False, False)
        try:
            ico = self._resource_path("swr_analysis_tool.ico")
            if ico.exists(): dlg.iconbitmap(default=str(ico))
        except Exception:
            pass
        frame = ttk.Frame(dlg)
        frame.pack(fill=tk.BOTH, expand=True, padx=16, pady=14)
        ttk.Label(
            frame,
            text="The scan will transmit at each frequency.\n\nUse low power and a suitable antenna or dummy load.\nRemain present at the radio while the sweep runs.",
            justify="left",
            wraplength=420,
        ).pack(anchor="w", pady=(0, 12))
        dont_show = tk.BooleanVar(value=False)
        ttk.Checkbutton(frame, text="Do not display this message again", variable=dont_show).pack(anchor="w", pady=(0, 12))
        result = {"ok": False}
        btns = ttk.Frame(frame)
        btns.pack(fill=tk.X)
        def yes():
            result["ok"] = True
            if dont_show.get():
                self.suppress_safety_var.set(True)
                self._save_settings()
            dlg.destroy()
        def no():
            result["ok"] = False
            dlg.destroy()
        ttk.Button(btns, text="Cancel", command=no).pack(side=tk.RIGHT, padx=(6, 0))
        ttk.Button(btns, text="Start Sweep", command=yes).pack(side=tk.RIGHT)
        dlg.protocol("WM_DELETE_WINDOW", no)
        self.wait_window(dlg)
        return result["ok"]

    def _cat_watchdog(self):
        """Poll CAT while idle so the GUI notices a lost radio/USB link."""
        try:
            scanning = bool(self.scan_thread and self.scan_thread.is_alive())
            if self.cat_connected and not scanning:
                try:
                    cat = self._cat()
                    fa = cat.read_freq()
                    cat.close()
                    if fa.startswith('FA') and len(fa) >= 12:
                        self._mark_cat_ok(fa)
                    else:
                        self._mark_cat_lost("CAT LOST")
                except Exception:
                    self._mark_cat_lost("CAT LOST")
        finally:
            self.after(3000, self._cat_watchdog)

    def _start_scan(self):
        if self.scan_thread and self.scan_thread.is_alive():
            return
        if not self.cat_connected:
            messagebox.showwarning("CAT Not Connected", "Connect/test CAT successfully before starting a sweep.")
            self.scan_var.set("Connect CAT before starting sweep")
            return
        # Preflight CAT check. This prevents false voice announcements if the
        # radio/USB CAT connection was lost after the earlier CAT test.
        try:
            cat = self._cat()
            fa = cat.read_freq()
            cat.close()
            if not (fa.startswith('FA') and len(fa) >= 12):
                raise RuntimeError(f"No valid frequency response: {fa or '<none>'}")
            self._mark_cat_ok(fa)
        except Exception as e:
            self._mark_cat_lost("CAT preflight failed")
            messagebox.showerror("CAT Not Ready", f"CAT is not responding. Reconnect from Setup or Connect/Test CAT.\n\n{e}")
            return
        if not self.suppress_safety_var.get():
            if not self._show_safety_dialog():
                return
        self.voice.say("Analysis Initiated")
        self.stop_event.clear(); self.scan_btn.config(state=tk.DISABLED); self.stop_btn.config(state=tk.NORMAL); self.results.clear(); self.tree.delete(*self.tree.get_children()); self._plot()
        self.scan_thread=threading.Thread(target=self._scan_worker,daemon=True); self.scan_thread.start()
    def _stop(self): self.stop_event.set(); self.scan_var.set("Stopping...")
    def _scan_worker(self):
        cat=None; saved_fa=""; saved_pc=""; saved_md=""; terminated_high_swr=False; failed=False
        try:
            start=parse_mhz(self.start_var.get()); stop=parse_mhz(self.stop_var.get()); step=int(float(self.step_var.get().replace(',', '.'))*1000)
            power=int(self.power_var.get()); dwell=float(self.settle_var.get().replace(',', '.')); samples=max(1,int(self.samples_var.get())); abort=float(self.max_swr_var.get().replace(',', '.'))
            if abort <= 1.0:
                raise ValueError("Abort SWR must be greater than 1.0.")
            if stop < start: start,stop=stop,start
            validate_sweep_inputs(start, stop, step)
            cat=self._cat(); saved_fa=cat.read_freq(); saved_pc=cat.read_power(); saved_md=cat.read_mode(); cat.set_mode_cwu(); cat.set_power(power)
            total=((stop-start)//step)+1; idx=0
            hz=start
            while hz<=stop and not self.stop_event.is_set():
                idx+=1
                cat.set_freq(hz)
                time.sleep(0.12)
                fa_now = cat.read_freq()
                display_hz = hz
                if fa_now.startswith('FA') and len(fa_now) >= 12:
                    try:
                        display_hz = int(fa_now[2:11])
                    except Exception:
                        display_hz = hz
                self.q.put(("status",f"Sweeping... {idx}/{total}",display_hz))
                self.q.put(("indicators", True, False, True, True)); cat.tx_on(); time.sleep(dwell)
                raws=[]; resp=""
                for _ in range(samples):
                    raw,resp=cat.read_swr_raw()
                    if raw is not None: raws.append(raw)
                    time.sleep(0.06)
                cat.tx_off(); self.q.put(("indicators", True, False, True, False)); time.sleep(0.08)
                raw=int(statistics.median(raws)) if raws else -1; swr=raw_to_swr(raw) if raw>=0 else 99.9
                status="OK" if raw>=0 else "No RM6 response"
                ri=cat.read_ri0();
                if ri: status += f" {ri}"
                pt=ScanPoint(hz,raw,swr,resp,status); self.q.put(("point",pt))
                if raw>=0 and swr>=abort:
                    terminated_high_swr = True
                    self.q.put(("voice", "High SWR detected"))
                    self.q.put(("voice", "Analysis Terminated"))
                    self.q.put(("log",f"Abort threshold reached: SWR {swr}")); break
                hz += step
        except Exception as e:
            failed = True
            self.q.put(("error",str(e)))
        finally:
            try:
                if cat:
                    cat.tx_off()
                    if saved_pc.startswith('PC'): cat.cmd(saved_pc)
                    if saved_md.startswith('MD'): cat.cmd(saved_md)
                    if saved_fa.startswith('FA'): cat.cmd(saved_fa)
                    cat.close()
            except Exception as e: self.q.put(("log",f"Restore warning: {e}"))
            self.q.put(("done", "failed" if failed else ("terminated" if terminated_high_swr else "completed")))
    def _pump(self):
        try:
            while True:
                typ,data,*rest=self.q.get_nowait()
                if typ=="status":
                    self.scan_var.set(data); self.led.set_frequency(fmt_freq(rest[0])); self.led.set_indicators(cat_ok=True, no_cat=False, scanning=True, tx=False)
                elif typ=="point":
                    p=data; self.results.append(p); self.tree.insert('',tk.END,values=(fmt_freq(p.frequency_hz),p.swr_raw,p.swr_estimate,p.status))
                    self.points_var.set(f"{len(self.results)} points"); self.swr_var.set(f"SWR {p.swr_estimate}"); self.raw_var.set(f"RAW {p.swr_raw}"); self._update_best(); self._plot()
                elif typ=="log": self._log(data)
                elif typ=="voice": self.voice.say(data)
                elif typ=="indicators": self.led.set_indicators(cat_ok=data, no_cat=rest[0], scanning=rest[1], tx=rest[2])
                elif typ=="error":
                    self.cat_connected = False
                    self.scan_btn.config(state=tk.DISABLED)
                    self._log("ERROR: "+data); self.led.set_frequency(None); self.led.set_indicators(cat_ok=False, no_cat=True, scanning=False, tx=False); self.status_var.set("CAT: ERROR"); self.cat_line.set(f"Radio   Port: {self.port_var.get().split()[0]}   Baud: {self.baud_var.get()}   Status: CAT LOST"); messagebox.showerror("Sweep Error", data)
                elif typ=="done":
                    self.stop_btn.config(state=tk.DISABLED)
                    if data == "failed":
                        self.cat_connected = False
                        self.scan_btn.config(state=tk.DISABLED)
                        self.scan_var.set("Analysis failed / reconnect CAT")
                        self._log("Analysis failed")
                        self.led.set_frequency(None)
                        self.led.set_indicators(cat_ok=False, no_cat=True, scanning=False, tx=False)
                    elif data == "terminated":
                        self.scan_btn.config(state=tk.NORMAL if self.cat_connected else tk.DISABLED)
                        self.scan_var.set("Analysis terminated / radio restored"); self._log("Analysis terminated"); self.led.set_indicators(cat_ok=True, no_cat=False, scanning=False, tx=False)
                    else:
                        self.scan_btn.config(state=tk.NORMAL if self.cat_connected else tk.DISABLED)
                        self.scan_var.set("Analysis complete / radio restored"); self._log("Analysis completed"); self.led.set_indicators(cat_ok=True, no_cat=False, scanning=False, tx=False); self.voice.say("Analysis completed")
        except queue.Empty: pass
        self.after(100,self._pump)
    def _update_best(self):
        good=[p for p in self.results if p.swr_raw>=0]
        if good:
            b=min(good,key=lambda p:p.swr_estimate); self.best_var.set(f"Best: {fmt_freq(b.frequency_hz)} SWR {b.swr_estimate}")
    def _plot(self):
        if not self.canvas: return
        self.ax.clear()
        self._style_crt_axes()
        good=[p for p in self.results if p.swr_raw>=0]
        if good:
            xs=[mhz_float(p.frequency_hz) for p in good]
            ymax = self._graph_upper_limit()
            ys=[max(1.0, min(ymax, p.swr_estimate)) for p in good]
            # Amber CRT phosphor trace: faint wide glow underneath, sharp line on top.
            self.ax.plot(xs, ys, color='#8a5200', linewidth=6, alpha=0.18, solid_capstyle='round')
            self.ax.plot(xs, ys, color='#ffb000', linewidth=2.0, marker='o', markersize=4,
                         markerfacecolor='#ffb000', markeredgecolor='#ffd166')
            b=min(good,key=lambda p:p.swr_estimate)
            bx=mhz_float(b.frequency_hz); by=max(1.0, min(ymax, b.swr_estimate))
            self.ax.scatter([bx],[by],s=110,color='#ffb000',edgecolors='#fff0b3',linewidths=1.0,zorder=5)
            self.ax.scatter([bx],[by],s=260,color='#8a5200',alpha=0.18,zorder=4)
        self.fig.tight_layout(pad=1.0)
        self.canvas.draw_idle()

    def _save_csv(self):
        if not self.results: messagebox.showinfo("No Data","No scan results yet."); return
        path=filedialog.asksaveasfilename(defaultextension='.csv',initialfile=self._default_export_name('csv'),filetypes=[('CSV','*.csv')])
        if not path: return
        with open(path,'w',newline='',encoding='utf-8') as f:
            w=csv.writer(f); w.writerow(['frequency_hz','frequency_display','swr_raw','swr_estimate','cat_response','status'])
            for p in self.results: w.writerow([p.frequency_hz,fmt_freq(p.frequency_hz),p.swr_raw,f'{p.swr_estimate:.1f}',p.response,p.status])
        self.csv_path_var.set(path); self._log(f"Saved CSV: {path}")
    def _save_png(self):
        if not self.canvas: return
        path=filedialog.asksaveasfilename(defaultextension='.png',initialfile=self._default_export_name('png'),filetypes=[('PNG','*.png')])
        if path: self.fig.savefig(path,facecolor=C_BG); self._log(f"Saved PNG: {path}")

    def _install_setting_traces(self):
        for name in ['port_var','baud_var','start_var','stop_var','step_var','power_var','settle_var','samples_var','csv_path_var','suppress_safety_var']:
            var = getattr(self, name, None)
            if hasattr(var, 'trace_add'):
                var.trace_add('write', lambda *_: self._save_settings())

        # Abort SWR controls both the safety threshold and the CRT graph's
        # upper Y-axis. Redraw the graph immediately when the value changes
        # while idle, so the operator sees the new scale without starting a
        # sweep or restarting the program. During an active sweep we keep the
        # display stable and the new value applies after the sweep ends.
        if hasattr(self.max_swr_var, 'trace_add'):
            self.max_swr_var.trace_add('write', self._on_abort_swr_changed)

        self.cat_line.set(f"Radio   Port: {self.port_var.get().split()[0] if self.port_var.get() else '--'}   Baud: {self.baud_var.get()}   Status: NO CAT DATA")

    def _on_abort_swr_changed(self, *_):
        self._save_settings()
        try:
            scanning = bool(self.scan_thread and self.scan_thread.is_alive())
        except Exception:
            scanning = False
        if not scanning and self.canvas:
            self._plot()

    def _load_settings(self):
        try:
            data=json.loads(SETTINGS.read_text())
            for k,v in data.items():
                var=getattr(self,k,None)
                if hasattr(var,'set'): var.set(v)
        except Exception: pass
    def _save_settings(self):
        data={k:getattr(self,k).get() for k in ['port_var','baud_var','start_var','stop_var','step_var','power_var','settle_var','samples_var','max_swr_var','csv_path_var','suppress_safety_var']}
        try: SETTINGS.write_text(json.dumps(data,indent=2))
        except Exception: pass
    def _on_close(self): self.stop_event.set(); self._save_settings(); self.destroy()

if __name__ == '__main__': App().mainloop()
