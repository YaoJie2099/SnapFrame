"""SnapFrame - a Lightshot-style screen capture + annotation tool for Windows.

Press the hotkey (PrintScreen / Ctrl+Alt+A by default), drag a region, annotate, then copy or save.
Everything stays on the local machine: there is deliberately no cloud upload feature.

Run:  python snapframe.py            (tray app)
      python snapframe.py --selftest (headless checks of rendering + clipboard)
"""
import ctypes
import ctypes.wintypes as wt
import datetime
import functools
import io
import json
import logging
import logging.handlers
import math
import os
import queue
import sys
import threading
import time
import tkinter as tk
from tkinter import filedialog

from PIL import Image, ImageColor, ImageDraw, ImageEnhance, ImageFont, ImageGrab, ImageTk

from branding import APP, VERSION, make_icon_image

# ----------------------------------------------------------------------------- paths / config

APPDATA = os.path.join(os.environ.get("APPDATA", os.path.expanduser("~")), APP)
CONFIG_PATH = os.path.join(APPDATA, "config.json")
LOG_PATH = os.path.join(APPDATA, "snapframe.log")
DEFAULT_SAVE_DIR = os.path.join(os.path.expanduser("~"), "Pictures", APP)
DEFAULTS = {
    "hotkeys": ["PrintScreen", "Ctrl+Alt+A"],
    "save_dir": DEFAULT_SAVE_DIR,
    "color": "#e53935",
    "width": 3,
    "format": "png",            # png | jpg | bmp  (default type in the Save dialog)
    "jpg_quality": 92,
}
IMAGE_TYPES = {"png": ("PNG image", "*.png"), "jpg": ("JPEG image", "*.jpg"), "bmp": ("BMP image", "*.bmp")}
log = logging.getLogger(APP)


def setup_logging():
    os.makedirs(APPDATA, exist_ok=True)
    h = logging.handlers.RotatingFileHandler(LOG_PATH, maxBytes=256 * 1024, backupCount=2, encoding="utf-8")
    h.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(message)s"))
    log.addHandler(h)
    log.setLevel(logging.INFO)


def load_config():
    cfg = dict(DEFAULTS)
    try:
        with open(CONFIG_PATH, "r", encoding="utf-8") as f:
            cfg.update(json.load(f))
    except FileNotFoundError:
        pass
    except Exception:
        log.exception("config unreadable, using defaults")
    return cfg


def save_config(cfg):
    try:
        os.makedirs(APPDATA, exist_ok=True)
        with open(CONFIG_PATH, "w", encoding="utf-8") as f:
            json.dump(cfg, f, indent=2)
    except Exception:
        log.exception("could not write config")


# ----------------------------------------------------------------------------- Windows helpers

user32 = ctypes.windll.user32
kernel32 = ctypes.windll.kernel32
user32.GetParent.restype = ctypes.c_void_p
user32.GetParent.argtypes = [ctypes.c_void_p]
user32.SetForegroundWindow.argtypes = [ctypes.c_void_p]
user32.OpenClipboard.argtypes = [ctypes.c_void_p]
user32.SetClipboardData.argtypes = [ctypes.c_uint, ctypes.c_void_p]
user32.SetClipboardData.restype = ctypes.c_void_p
kernel32.GlobalAlloc.argtypes = [ctypes.c_uint, ctypes.c_size_t]
kernel32.GlobalAlloc.restype = ctypes.c_void_p
kernel32.GlobalLock.argtypes = [ctypes.c_void_p]
kernel32.GlobalLock.restype = ctypes.c_void_p
kernel32.GlobalUnlock.argtypes = [ctypes.c_void_p]
kernel32.GlobalFree.argtypes = [ctypes.c_void_p]


def set_dpi_aware():
    """Capture in physical pixels so the screenshot and the overlay line up on scaled displays."""
    try:
        user32.SetProcessDpiAwarenessContext(ctypes.c_void_p(-4))      # per-monitor v2
        return
    except Exception:
        pass
    try:
        ctypes.windll.shcore.SetProcessDpiAwareness(2)
    except Exception:
        try:
            user32.SetProcessDPIAware()
        except Exception:
            pass


def virtual_screen():
    """(left, top, width, height) of the whole multi-monitor desktop."""
    return (user32.GetSystemMetrics(76), user32.GetSystemMetrics(77),
            user32.GetSystemMetrics(78), user32.GetSystemMetrics(79))


def force_foreground(tkwin):
    """A window opened from a global hotkey doesn't get keyboard focus by default; nudge it."""
    try:
        hwnd = user32.GetParent(tkwin.winfo_id()) or tkwin.winfo_id()
        user32.keybd_event(0x12, 0, 0, 0)          # tap Alt so SetForegroundWindow is allowed
        user32.keybd_event(0x12, 0, 2, 0)
        user32.SetForegroundWindow(hwnd)
    except Exception:
        log.exception("force_foreground failed")


def copy_image_to_clipboard(img):
    """Put a PIL image on the Windows clipboard as CF_DIB (pastes into Word, Teams, Outlook, Paint...)."""
    buf = io.BytesIO()
    img.convert("RGB").save(buf, "BMP")
    data = buf.getvalue()[14:]                      # strip the 14-byte BITMAPFILEHEADER
    h = kernel32.GlobalAlloc(0x0002, len(data))     # GMEM_MOVEABLE
    if not h:
        raise OSError("GlobalAlloc failed")
    p = kernel32.GlobalLock(h)
    ctypes.memmove(p, data, len(data))
    kernel32.GlobalUnlock(h)
    for _ in range(10):                             # clipboard may be briefly held by another app
        if user32.OpenClipboard(None):
            break
        time.sleep(0.05)
    else:
        kernel32.GlobalFree(h)
        raise OSError("OpenClipboard failed")
    try:
        user32.EmptyClipboard()
        if not user32.SetClipboardData(8, h):       # CF_DIB
            kernel32.GlobalFree(h)
            raise OSError("SetClipboardData failed")
    finally:
        user32.CloseClipboard()


RUN_KEY = r"Software\Microsoft\Windows\CurrentVersion\Run"


def autostart_enabled():
    import winreg
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, RUN_KEY) as k:
            winreg.QueryValueEx(k, APP)
            return True
    except OSError:
        return False


def set_autostart(on):
    import winreg
    with winreg.OpenKey(winreg.HKEY_CURRENT_USER, RUN_KEY, 0, winreg.KEY_SET_VALUE) as k:
        if on:
            cmd = f'"{sys.executable}"' if getattr(sys, "frozen", False) \
                else f'"{sys.executable}" "{os.path.abspath(__file__)}"'
            winreg.SetValueEx(k, APP, 0, winreg.REG_SZ, cmd)
        else:
            try:
                winreg.DeleteValue(k, APP)
            except OSError:
                pass


# ----------------------------------------------------------------------------- global hotkeys

MODS = {"alt": 0x1, "ctrl": 0x2, "control": 0x2, "shift": 0x4, "win": 0x8}
SPECIAL = {"printscreen": 0x2C, "prtsc": 0x2C, "snapshot": 0x2C, "pause": 0x13, "space": 0x20,
           "insert": 0x2D, "home": 0x24, "end": 0x23}


def parse_hotkey(text):
    mods, vk = 0, None
    for part in (p.strip().lower() for p in text.split("+")):
        if part in MODS:
            mods |= MODS[part]
        elif part in SPECIAL:
            vk = SPECIAL[part]
        elif len(part) == 1 and part.isalnum():
            vk = ord(part.upper())
        elif part.startswith("f") and part[1:].isdigit() and 1 <= int(part[1:]) <= 24:
            vk = 0x70 + int(part[1:]) - 1
        else:
            raise ValueError(f"unknown key '{part}' in hotkey '{text}'")
    if vk is None:
        raise ValueError(f"no key in hotkey '{text}'")
    return mods, vk


class HotkeyThread(threading.Thread):
    """RegisterHotKey needs the message loop on the same thread that registered the key."""

    def __init__(self, hotkeys, out_queue):
        super().__init__(daemon=True)
        self.hotkeys, self.q = hotkeys, out_queue
        self.registered, self.failed = [], []
        self.ready = threading.Event()

    def run(self):
        for i, text in enumerate(self.hotkeys, 1):
            try:
                mods, vk = parse_hotkey(text)
                if user32.RegisterHotKey(None, i, mods | 0x4000, vk):      # MOD_NOREPEAT
                    self.registered.append((i, text))
                else:
                    self.failed.append(text)
                    log.warning("RegisterHotKey failed for %s (err %s)", text, ctypes.GetLastError())
            except ValueError as e:
                self.failed.append(text)
                log.warning("%s", e)
        self.ready.set()
        msg = wt.MSG()
        while user32.GetMessageW(ctypes.byref(msg), None, 0, 0) > 0:
            if msg.message == 0x0312:                                       # WM_HOTKEY
                self.q.put("capture")
        for i, _ in self.registered:
            user32.UnregisterHotKey(None, i)

    def stop(self):
        if self.is_alive():
            user32.PostThreadMessageW(self.native_id, 0x0012, 0, 0)        # WM_QUIT


# ----------------------------------------------------------------------------- annotation rendering

@functools.lru_cache(maxsize=64)
def get_font(px):
    for name in ("msyh.ttc", "segoeui.ttf", "arial.ttf"):      # YaHei first: Latin + CJK
        try:
            return ImageFont.truetype(name, px)
        except OSError:
            continue
    return ImageFont.load_default(px)


def draw_item(d, it, s, ox, oy, marker_pass=False):
    """Draw one annotation. Coordinates are stored in screen space; (ox, oy) is the crop origin."""
    pts = [((x - ox) * s, (y - oy) * s) for x, y in it["pts"]]
    col = ImageColor.getrgb(it["color"])
    t = it["tool"]
    w = max(1, round(it["w"] * s * (4 if t == "marker" else 1)))
    if t in ("pen", "marker"):
        if len(pts) > 1:
            d.line(pts, fill=col, width=w, joint="curve")
        r = w / 2
        for (x, y) in (pts[0], pts[-1]):
            d.ellipse([x - r, y - r, x + r, y + r], fill=col)
    elif t == "line":
        d.line([pts[0], pts[-1]], fill=col, width=w)
        r = w / 2
        for (x, y) in (pts[0], pts[-1]):
            d.ellipse([x - r, y - r, x + r, y + r], fill=col)
    elif t == "arrow":
        (x0, y0), (x1, y1) = pts[0], pts[-1]
        ang = math.atan2(y1 - y0, x1 - x0)
        head = max(12 * s, w * 4.5)
        if math.hypot(x1 - x0, y1 - y0) < 2:
            return
        bx, by = x1 - math.cos(ang) * head * 0.8, y1 - math.sin(ang) * head * 0.8
        d.line([(x0, y0), (bx, by)], fill=col, width=w)
        wing = math.radians(24)
        p1 = (x1 - math.cos(ang - wing) * head, y1 - math.sin(ang - wing) * head)
        p2 = (x1 - math.cos(ang + wing) * head, y1 - math.sin(ang + wing) * head)
        d.polygon([(x1, y1), p1, p2], fill=col)
    elif t in ("rect", "ellipse"):
        (x0, y0), (x1, y1) = pts[0], pts[-1]
        box = [min(x0, x1), min(y0, y1), max(x0, x1), max(y0, y1)]
        if t == "rect":
            d.rectangle(box, outline=col, width=w)
        else:
            d.ellipse(box, outline=col, width=w)
    elif t == "text":
        d.text(pts[0], it["text"], fill=col, font=get_font(max(6, int(it["size"] * s))))


def render_annotations(base, items, ox, oy, scale=1):
    """Return `base` (RGB crop) with the annotations drawn on top. Supersamples when scale > 1."""
    W, H = base.size
    acc = Image.new("RGBA", (W * scale, H * scale), (0, 0, 0, 0))
    d = ImageDraw.Draw(acc)
    for it in items:
        if it["tool"] == "marker":                                  # highlighter: translucent
            tmp = Image.new("RGBA", acc.size, (0, 0, 0, 0))
            draw_item(ImageDraw.Draw(tmp), it, scale, ox, oy)
            tmp.putalpha(tmp.getchannel("A").point(lambda v: int(v * 0.4)))
            acc = Image.alpha_composite(acc, tmp)
            d = ImageDraw.Draw(acc)
        else:
            draw_item(d, it, scale, ox, oy)
    if scale > 1:
        acc = acc.resize((W, H), Image.LANCZOS)
    out = base.convert("RGBA")
    out.alpha_composite(acc)
    return out.convert("RGB")


# ----------------------------------------------------------------------------- capture overlay

HANDLES = {"nw": (0, 0), "n": (.5, 0), "ne": (1, 0), "e": (1, .5),
           "se": (1, 1), "s": (.5, 1), "sw": (0, 1), "w": (0, .5)}
HANDLE_CURSORS = {"nw": "size_nw_se", "se": "size_nw_se", "ne": "size_ne_sw", "sw": "size_ne_sw",
                  "n": "sb_v_double_arrow", "s": "sb_v_double_arrow",
                  "e": "sb_h_double_arrow", "w": "sb_h_double_arrow"}
TOOLS = [("select", "Select"), ("pen", "Pen"), ("line", "Line"), ("arrow", "Arrow"),
         ("rect", "Rect"), ("ellipse", "Ellipse"), ("marker", "Marker"), ("text", "Text")]
COLORS = ["#e53935", "#fb8c00", "#fdd835", "#43a047", "#1e88e5", "#8e24aa", "#000000", "#ffffff"]
TB_BG, TB_BTN, TB_ACT = "#2b2b2b", "#3c3c3c", "#1e88e5"


class CaptureOverlay:
    def __init__(self, app, shot, origin):
        self.app, self.shot = app, shot.convert("RGB")
        self.vx, self.vy = origin
        self.W, self.H = self.shot.size
        self.sel = None                       # (x0, y0, x1, y1) in canvas coords
        self.tool = "select"
        self.color, self.width = app.cfg["color"], int(app.cfg["width"])
        self.items, self.cur, self.drag = [], None, None
        self.entry = self.entry_win = None
        self.closed = False

        self.top = tk.Toplevel(app.root)
        self.top.overrideredirect(True)
        self.top.attributes("-topmost", True)
        self.top.geometry(f"{self.W}x{self.H}{self.vx:+d}{self.vy:+d}")
        self.cv = tk.Canvas(self.top, width=self.W, height=self.H, highlightthickness=0, bd=0,
                            cursor="crosshair", bg="black")
        self.cv.pack()
        self.bg_tk = ImageTk.PhotoImage(ImageEnhance.Brightness(self.shot).enhance(0.45))
        self.cv.create_image(0, 0, anchor="nw", image=self.bg_tk)
        self.hint = self.cv.create_text(
            24, 24, anchor="nw", fill="white", font=("Segoe UI", 11),
            text="Drag to select a region   |   Esc = cancel   |   Right-click = cancel")

        self.build_toolbar()
        c = self.cv
        c.bind("<ButtonPress-1>", self.on_press)
        c.bind("<B1-Motion>", self.on_motion)
        c.bind("<ButtonRelease-1>", self.on_release)
        c.bind("<Motion>", self.on_hover)
        c.bind("<Double-Button-1>", self.on_double)
        c.bind("<Button-3>", lambda e: self.close())
        c.bind("<MouseWheel>", self.on_wheel)
        for t in (self.top, c):
            t.bind("<Escape>", lambda e: self.on_escape())
            t.bind("<Return>", lambda e: self.key_guard(self.copy_and_close))
            t.bind("<Control-c>", lambda e: self.key_guard(self.copy_and_close))
            t.bind("<Control-C>", lambda e: self.key_guard(self.copy_and_close))
            t.bind("<Control-s>", lambda e: self.key_guard(self.save))
            t.bind("<Control-S>", lambda e: self.key_guard(self.save))
            t.bind("<Control-z>", lambda e: self.key_guard(self.undo))
            t.bind("<Control-Z>", lambda e: self.key_guard(self.undo))
        self.top.update()
        force_foreground(self.top)
        c.focus_force()

    # ---- helpers
    def key_guard(self, fn):
        if self.entry is None and self.sel is not None:
            fn()

    def clamp(self, x, y):
        return max(0, min(self.W - 1, x)), max(0, min(self.H - 1, y))

    def inside(self, x, y):
        s = self.sel
        return bool(s) and s[0] <= x <= s[2] and s[1] <= y <= s[3]

    def hit_handle(self, x, y):
        if not self.sel:
            return None
        s = self.sel
        for name, (fx, fy) in HANDLES.items():
            hx, hy = s[0] + fx * (s[2] - s[0]), s[1] + fy * (s[3] - s[1])
            if abs(x - hx) <= 7 and abs(y - hy) <= 7:
                return name
        return None

    def norm(self, a):
        x0, y0, x1, y1 = a
        x0, y0 = self.clamp(x0, y0)
        x1, y1 = self.clamp(x1, y1)
        return (min(x0, x1), min(y0, y1), max(x0, x1), max(y0, y1))

    # ---- toolbar
    def build_toolbar(self):
        f = tk.Frame(self.top, bg=TB_BG, padx=4, pady=4)
        self.tool_btns, self.color_btns = {}, {}
        r1 = tk.Frame(f, bg=TB_BG)
        r1.pack(anchor="w")
        r2 = tk.Frame(f, bg=TB_BG)
        r2.pack(anchor="w", pady=(4, 0))

        def mk(parent, text, cmd, **kw):
            b = tk.Button(parent, text=text, command=cmd, bg=kw.pop("bg", TB_BTN), fg="white", bd=0,
                          activebackground="#5a5a5a", activeforeground="white", relief="flat",
                          font=("Segoe UI", 9), padx=8, pady=3, takefocus=0, **kw)
            b.pack(side="left", padx=1)
            return b

        for key, label in TOOLS:
            self.tool_btns[key] = mk(r1, label, lambda k=key: self.set_tool(k))
        mk(r1, "Undo", self.undo)
        for col in COLORS:
            b = tk.Button(r2, bg=col, width=2, bd=2, relief="flat", takefocus=0,
                          activebackground=col, command=lambda c=col: self.set_color(c))
            b.pack(side="left", padx=2)
            self.color_btns[col] = b
        mk(r2, "–", lambda: self.bump_width(-1))
        self.width_lbl = tk.Label(r2, text=str(self.width), bg=TB_BG, fg="white", width=3, font=("Segoe UI", 9))
        self.width_lbl.pack(side="left")
        mk(r2, "+", lambda: self.bump_width(1))
        mk(r2, "Copy", self.copy_and_close, bg="#2e7d32")
        mk(r2, "Save", self.save, bg="#1565c0")
        mk(r2, "✕", self.close, bg="#b71c1c")
        self.toolbar = f
        self.tb_item = self.cv.create_window(0, 0, anchor="nw", window=f, state="hidden")
        self.refresh_toolbar()

    def refresh_toolbar(self):
        for k, b in self.tool_btns.items():
            b.configure(bg=TB_ACT if k == self.tool else TB_BTN)
        for c, b in self.color_btns.items():
            b.configure(relief="solid" if c == self.color else "flat", bd=3 if c == self.color else 2)
        self.width_lbl.configure(text=str(self.width))

    def place_toolbar(self):
        if not self.sel or self.drag:
            self.cv.itemconfigure(self.tb_item, state="hidden")
            return
        self.toolbar.update_idletasks()
        tw, th = self.toolbar.winfo_reqwidth(), self.toolbar.winfo_reqheight()
        x0, y0, x1, y1 = self.sel
        x = max(0, min(self.W - tw, x1 - tw))
        if y1 + 8 + th <= self.H:
            y = y1 + 8
        elif y0 - 8 - th >= 0:
            y = y0 - 8 - th
        else:
            y = max(0, y1 - th - 8)
        self.cv.coords(self.tb_item, x, y)
        self.cv.itemconfigure(self.tb_item, state="normal")
        self.cv.tag_raise(self.tb_item)

    def set_tool(self, key):
        self.commit_text()
        self.tool = key
        self.refresh_toolbar()
        self.cv.configure(cursor="crosshair" if key != "text" else "xterm")

    def set_color(self, col):
        self.color = col
        self.refresh_toolbar()
        if self.entry:
            self.entry.configure(fg=col, insertbackground=col)

    def bump_width(self, d):
        self.width = max(1, min(20, self.width + d))
        self.refresh_toolbar()

    def on_wheel(self, e):
        self.bump_width(1 if e.delta > 0 else -1)

    # ---- mouse
    def on_press(self, e):
        self.commit_text()
        x, y = self.clamp(e.x, e.y)
        if self.tool == "select":
            if self.sel:
                h = self.hit_handle(x, y)
                if h:
                    self.drag = ("resize", h, x, y, self.sel)
                    return self.redraw()
                if self.inside(x, y):
                    self.drag = ("move", x, y, self.sel)
                    return self.redraw()
            self.items.clear()
            self.sel = (x, y, x, y)
            self.drag = ("new", x, y)
            self.cv.itemconfigure(self.hint, state="hidden")
            return self.redraw()
        if not self.inside(x, y):
            return
        if self.tool == "text":
            return self.start_text(x, y)
        self.cur = {"tool": self.tool, "pts": [(x, y)] if self.tool in ("pen", "marker") else [(x, y), (x, y)],
                    "color": self.color, "w": self.width}
        self.drag = ("draw",)
        self.redraw()

    def on_motion(self, e):
        if not self.drag:
            return
        x, y = self.clamp(e.x, e.y)
        kind = self.drag[0]
        if kind == "new":
            self.sel = self.norm((self.drag[1], self.drag[2], x, y))
        elif kind == "move":
            _, sx, sy, o = self.drag
            dx = max(-o[0], min(self.W - 1 - o[2], x - sx))
            dy = max(-o[1], min(self.H - 1 - o[3], y - sy))
            self.sel = (o[0] + dx, o[1] + dy, o[2] + dx, o[3] + dy)
        elif kind == "resize":
            _, name, sx, sy, o = self.drag
            x0, y0, x1, y1 = o
            dx, dy = x - sx, y - sy
            if "w" in name: x0 += dx
            if "e" in name: x1 += dx
            if "n" in name: y0 += dy
            if "s" in name: y1 += dy
            self.sel = self.norm((x0, y0, x1, y1))
        elif kind == "draw" and self.cur:
            if self.cur["tool"] in ("pen", "marker"):
                self.cur["pts"].append((x, y))
            else:
                x, y = self.clamp(*self.limit_to_sel(x, y))
                self.cur["pts"][-1] = (x, y)
        self.redraw()

    def limit_to_sel(self, x, y):
        s = self.sel
        return max(s[0], min(s[2], x)), max(s[1], min(s[3], y))

    def on_release(self, e):
        if not self.drag:
            return
        kind = self.drag[0]
        self.drag = None
        if kind == "draw" and self.cur:
            it = self.cur
            self.cur = None
            if it["tool"] in ("pen", "marker") or math.dist(it["pts"][0], it["pts"][-1]) > 2:
                self.items.append(it)
        elif kind == "new" and self.sel and (self.sel[2] - self.sel[0] < 3 or self.sel[3] - self.sel[1] < 3):
            self.sel = None                     # a click, not a drag
            self.cv.itemconfigure(self.hint, state="normal")
        self.redraw()

    def on_hover(self, e):
        if self.drag or self.tool != "select":
            return
        h = self.hit_handle(e.x, e.y)
        self.cv.configure(cursor=HANDLE_CURSORS[h] if h else ("fleur" if self.inside(e.x, e.y) else "crosshair"))

    def on_double(self, e):
        if self.tool == "select" and self.inside(e.x, e.y):
            self.copy_and_close()

    def on_escape(self):
        if self.entry:
            self.cancel_text()
        else:
            self.close()

    # ---- text tool
    def start_text(self, x, y):
        size = 10 + self.width * 3
        lum = sum(ImageColor.getrgb(self.color)) / 3
        e = tk.Entry(self.cv, font=("Microsoft YaHei UI", -size), fg=self.color, insertbackground=self.color,
                     bg="#2b2b2b" if lum > 140 else "#f5f5f5", bd=0, relief="flat", highlightthickness=1,
                     highlightbackground="#1e88e5", highlightcolor="#1e88e5")
        self.entry, self.entry_pos, self.entry_size = e, (x, y), size
        self.entry_win = self.cv.create_window(x, y, anchor="nw", window=e, width=260)
        e.bind("<Return>", lambda ev: self.commit_text() or "break")
        e.bind("<Escape>", lambda ev: self.cancel_text() or "break")
        e.focus_set()

    def commit_text(self):
        e = self.entry
        if not e:
            return
        text = e.get().strip()
        x, y = self.entry_pos
        self.entry = None
        self.cv.delete(self.entry_win)
        e.destroy()
        if text:
            self.items.append({"tool": "text", "pts": [(x + 2, y + 2)], "text": text, "color": self.color,
                               "w": self.width, "size": self.entry_size})
        self.cv.focus_force()
        self.redraw()

    def cancel_text(self):
        e = self.entry
        if e:
            self.entry = None
            self.cv.delete(self.entry_win)
            e.destroy()
            self.cv.focus_force()

    # ---- drawing
    def undo(self):
        if self.items:
            self.items.pop()
            self.redraw()

    def compose(self, scale=1):
        x0, y0, x1, y1 = self.sel
        base = self.shot.crop((x0, y0, x1 + 1, y1 + 1))
        items = self.items + ([self.cur] if self.cur else [])
        return render_annotations(base, items, x0, y0, scale)

    def final_image(self):
        x0, y0, x1, y1 = self.sel
        area = (x1 - x0 + 1) * (y1 - y0 + 1)
        return self.compose(2 if area <= 4_000_000 else 1)

    def redraw(self):
        cv = self.cv
        cv.delete("dyn")
        if self.sel:
            x0, y0, x1, y1 = self.sel
            self.sel_tk = ImageTk.PhotoImage(self.compose())
            cv.create_image(x0, y0, anchor="nw", image=self.sel_tk, tags="dyn")
            cv.create_rectangle(x0, y0, x1, y1, outline="#1e88e5", width=1, tags="dyn")
            for fx, fy in HANDLES.values():
                hx, hy = x0 + fx * (x1 - x0), y0 + fy * (y1 - y0)
                cv.create_rectangle(hx - 3, hy - 3, hx + 3, hy + 3, fill="white", outline="#1e88e5", tags="dyn")
            label = f"{x1 - x0 + 1} × {y1 - y0 + 1}"
            ly = y0 - 24 if y0 >= 24 else y0 + 4
            cv.create_rectangle(x0, ly, x0 + 9 * len(label) + 12, ly + 20, fill="#1e1e1e", outline="", tags="dyn")
            cv.create_text(x0 + 6, ly + 10, anchor="w", text=label, fill="white", font=("Segoe UI", 9), tags="dyn")
        cv.tag_raise(self.tb_item)
        self.place_toolbar()

    # ---- actions
    def copy_and_close(self):
        if not self.sel:
            return
        try:
            copy_image_to_clipboard(self.final_image())
        except Exception:
            log.exception("copy failed")
            self.app.notify("Copy to clipboard failed - see log")
        self.close()

    def save(self):
        if not self.sel:
            return
        img = self.final_image()
        self.top.withdraw()
        os.makedirs(self.app.cfg["save_dir"], exist_ok=True)
        fmt = self.app.cfg.get("format", "png").lower()
        fmt = fmt if fmt in IMAGE_TYPES else "png"
        name = datetime.datetime.now().strftime(f"{APP}_%Y%m%d_%H%M%S.{fmt}")
        order = [fmt] + [k for k in IMAGE_TYPES if k != fmt]
        path = filedialog.asksaveasfilename(
            title="Save screenshot", initialdir=self.app.cfg["save_dir"], initialfile=name,
            defaultextension="." + fmt, filetypes=[IMAGE_TYPES[k] for k in order])
        if not path:
            self.top.deiconify()
            force_foreground(self.top)
            self.cv.focus_force()
            return
        try:
            ext = os.path.splitext(path)[1].lower()
            if ext in (".jpg", ".jpeg"):
                img.save(path, "JPEG", quality=int(self.app.cfg.get("jpg_quality", 92)))
            elif ext == ".bmp":
                img.save(path, "BMP")
            else:
                img.save(path, "PNG")
            self.app.cfg["save_dir"] = os.path.dirname(path)
            self.app.cfg["color"], self.app.cfg["width"] = self.color, self.width
            save_config(self.app.cfg)
            self.app.notify(f"Saved: {os.path.basename(path)}")
        except Exception:
            log.exception("save failed")
            self.app.notify("Save failed - see log")
        self.close()

    def close(self):
        if self.closed:
            return
        self.closed = True
        self.app.cfg["color"], self.app.cfg["width"] = self.color, self.width
        save_config(self.app.cfg)
        self.top.destroy()
        self.app.overlay = None


# ----------------------------------------------------------------------------- options dialog

IGNORED_KEYSYMS = {"Shift_L", "Shift_R", "Control_L", "Control_R", "Alt_L", "Alt_R", "Win_L", "Win_R",
                   "Super_L", "Super_R", "Meta_L", "Meta_R", "Caps_Lock", "Num_Lock", "Scroll_Lock",
                   "Tab", "ISO_Left_Tab", "Escape", "Return", "BackSpace", "Delete"}
KEYSYM_NAMES = {"Print": "PrintScreen", "Pause": "Pause", "space": "Space", "Insert": "Insert",
                "Home": "Home", "End": "End"}


def hotkey_from_event(e):
    """Build 'Ctrl+Alt+A' from a Tk key event; None for modifier-only / unsupported keys."""
    ks = e.keysym
    if ks in IGNORED_KEYSYMS:
        return None
    if ks in KEYSYM_NAMES:
        key = KEYSYM_NAMES[ks]
    elif len(ks) == 1 and ks.isalnum():
        key = ks.upper()
    elif ks.startswith("F") and ks[1:].isdigit() and 1 <= int(ks[1:]) <= 24:
        key = ks
    else:
        return None
    down = lambda vk: user32.GetAsyncKeyState(vk) & 0x8000
    mods = [n for n, on in (("Ctrl", down(0x11)), ("Alt", down(0x12)), ("Shift", down(0x10)),
                            ("Win", down(0x5B) or down(0x5C))) if on]
    return "+".join(mods + [key])


def hotkey_problem(text):
    """Why a hotkey string is not acceptable, or None if it is fine."""
    try:
        mods, vk = parse_hotkey(text)
    except ValueError as e:
        return str(e)
    if not mods and not (vk == 0x2C or vk == 0x13 or 0x70 <= vk <= 0x87):
        return f"'{text}' has no Ctrl/Alt/Shift/Win - it would block normal typing. Add a modifier."
    return None


class OptionsDialog:
    def __init__(self, app):
        self.app = app
        self.old_hotkeys = list(app.cfg["hotkeys"])
        app.hk.stop()                       # free the hotkeys so they can be pressed to record them
        app.hk.join(1)
        w = self.win = tk.Toplevel(app.root)
        w.title(f"{APP} Options")
        w.resizable(False, False)
        w.attributes("-topmost", True)
        w.protocol("WM_DELETE_WINDOW", self.cancel)
        from tkinter import ttk
        f = ttk.Frame(w, padding=14)
        f.pack()

        ttk.Label(f, text="Capture hotkeys", font=("Segoe UI", 10, "bold")).grid(row=0, column=0, columnspan=3, sticky="w")
        ttk.Label(f, text="Click a box, then press the key combination.", foreground="#555").grid(
            row=1, column=0, columnspan=3, sticky="w", pady=(0, 6))
        self.hk_vars = []
        olds = self.old_hotkeys + [""] * 2
        for i in range(2):
            var = tk.StringVar(value=olds[i])
            self.hk_vars.append(var)
            ttk.Label(f, text=f"Hotkey {i + 1}:").grid(row=2 + i, column=0, sticky="w", pady=2)
            e = ttk.Entry(f, textvariable=var, width=24, state="readonly", justify="center")
            e.grid(row=2 + i, column=1, padx=6)
            e.bind("<KeyPress>", lambda ev, v=var: self.record(ev, v))
            e.bind("<Button-1>", lambda ev: ev.widget.focus_set())
            ttk.Button(f, text="Clear", width=7, command=lambda v=var: v.set("")).grid(row=2 + i, column=2)
        self.msg = ttk.Label(f, text="", foreground="#b71c1c", wraplength=330, justify="left")
        self.msg.grid(row=4, column=0, columnspan=3, sticky="w", pady=(4, 0))

        ttk.Separator(f).grid(row=5, column=0, columnspan=3, sticky="we", pady=10)
        ttk.Label(f, text="Saving", font=("Segoe UI", 10, "bold")).grid(row=6, column=0, columnspan=3, sticky="w")
        ttk.Label(f, text="Image format:").grid(row=7, column=0, sticky="w", pady=2)
        self.fmt = tk.StringVar(value=app.cfg.get("format", "png").upper())
        cb = ttk.Combobox(f, textvariable=self.fmt, values=["PNG", "JPG", "BMP"], state="readonly", width=8)
        cb.grid(row=7, column=1, sticky="w", padx=6)
        ttk.Label(f, text="JPG quality:").grid(row=8, column=0, sticky="w", pady=2)
        self.q = tk.IntVar(value=int(app.cfg.get("jpg_quality", 92)))
        ttk.Spinbox(f, from_=40, to=100, textvariable=self.q, width=6).grid(row=8, column=1, sticky="w", padx=6)
        ttk.Label(f, text="Save folder:").grid(row=9, column=0, sticky="w", pady=2)
        self.dir = tk.StringVar(value=app.cfg["save_dir"])
        ttk.Entry(f, textvariable=self.dir, width=24).grid(row=9, column=1, padx=6)
        ttk.Button(f, text="Browse...", width=9, command=self.browse).grid(row=9, column=2)

        bar = ttk.Frame(f)
        bar.grid(row=10, column=0, columnspan=3, sticky="e", pady=(14, 0))
        ttk.Button(bar, text="Save", command=self.ok).pack(side="left", padx=4)
        ttk.Button(bar, text="Cancel", command=self.cancel).pack(side="left")
        w.update_idletasks()
        sw, sh = w.winfo_screenwidth(), w.winfo_screenheight()
        w.geometry(f"+{(sw - w.winfo_reqwidth()) // 2}+{(sh - w.winfo_reqheight()) // 3}")
        force_foreground(w)

    def record(self, ev, var):
        if ev.keysym in ("BackSpace", "Delete"):
            var.set("")
            return "break"
        if ev.keysym in ("Tab", "ISO_Left_Tab"):
            return None                     # let Tab move focus
        text = hotkey_from_event(ev)
        if text:
            var.set(text)
            self.msg.configure(text="")
        return "break"

    def browse(self):
        d = filedialog.askdirectory(parent=self.win, initialdir=self.dir.get())
        if d:
            self.dir.set(os.path.normpath(d))

    def close(self):
        self.win.destroy()
        self.app.options = None

    def cancel(self):
        self.app.restart_hotkeys(self.old_hotkeys)
        self.close()

    def ok(self):
        keys = [v.get().strip() for v in self.hk_vars if v.get().strip()]
        if not keys:
            return self.msg.configure(text="Set at least one hotkey.")
        if len({k.lower() for k in keys}) != len(keys):
            return self.msg.configure(text="The two hotkeys are the same.")
        for k in keys:
            p = hotkey_problem(k)
            if p:
                return self.msg.configure(text=p)
        try:
            quality = max(40, min(100, int(self.q.get())))
        except (tk.TclError, ValueError):
            return self.msg.configure(text="JPG quality must be a number from 40 to 100.")
        failed = self.app.restart_hotkeys(keys)
        if failed:
            self.app.restart_hotkeys(self.old_hotkeys)          # roll back to what worked
            return self.msg.configure(
                text=f"Already used by another program: {', '.join(failed)}. Pick a different combination.")
        self.app.cfg.update({"hotkeys": keys, "format": self.fmt.get().lower(), "jpg_quality": quality,
                             "save_dir": self.dir.get().strip() or DEFAULT_SAVE_DIR})
        save_config(self.app.cfg)
        self.app.notify(f"Saved. Capture with {', '.join(keys)}.")
        self.close()


# ----------------------------------------------------------------------------- tray app

class App:
    def __init__(self, capture_now=False):
        self.cfg = load_config()
        self.q = queue.Queue()
        self.overlay = None
        self.options = None
        self.root = tk.Tk()
        self.root.withdraw()
        self.hk = None
        self.restart_hotkeys(self.cfg["hotkeys"])
        self.icon = None
        try:
            import pystray
            menu = pystray.Menu(
                pystray.MenuItem("Capture", lambda: self.q.put("capture_delayed"), default=True),
                pystray.MenuItem("Open screenshots folder", lambda: self.q.put("open_folder")),
                pystray.MenuItem("Options...", lambda: self.q.put("options")),
                pystray.MenuItem("Start with Windows", lambda: self.q.put("toggle_autostart"),
                                 checked=lambda item: autostart_enabled()),
                pystray.Menu.SEPARATOR,
                pystray.MenuItem(f"{APP} {VERSION}", None, enabled=False),
                pystray.MenuItem("Quit", lambda: self.q.put("quit")))
            self.icon = pystray.Icon(APP, make_icon_image(64), APP, menu)
            self.icon.run_detached()
        except Exception:
            log.exception("tray icon unavailable")
        keys = ", ".join(t for _, t in self.hk.registered) or "none"
        log.info("%s %s started; hotkeys registered: %s; failed: %s", APP, VERSION, keys, self.hk.failed or "-")
        if self.hk.failed:
            self.root.after(1500, lambda: self.notify(
                f"Hotkey unavailable: {', '.join(self.hk.failed)} (taken by another app). Active: {keys}"))
        elif self.cfg.get("first_run", True):
            self.cfg["first_run"] = False
            save_config(self.cfg)
            self.root.after(1500, lambda: self.notify(f"Running in the tray. Press {keys} to capture."))
        self.root.after(80, self.poll)
        if capture_now:
            self.root.after(300, self.capture)

    def restart_hotkeys(self, hotkeys):
        """(Re)register the global hotkeys; returns the list that could not be registered."""
        if self.hk:
            self.hk.stop()
            self.hk.join(1)
        self.hk = HotkeyThread(list(hotkeys), self.q)
        self.hk.start()
        self.hk.ready.wait(2)
        log.info("hotkeys: registered %s, failed %s", [t for _, t in self.hk.registered], self.hk.failed)
        return list(self.hk.failed)

    def open_options(self):
        if self.options:
            self.options.win.lift()
            force_foreground(self.options.win)
        elif not self.overlay:
            self.options = OptionsDialog(self)

    def notify(self, msg):
        log.info("notify: %s", msg)
        if self.icon:
            try:
                self.icon.notify(msg, APP)
            except Exception:
                pass

    def poll(self):
        try:
            while True:
                cmd = self.q.get_nowait()
                if cmd == "capture":
                    self.capture()
                elif cmd == "capture_delayed":
                    self.root.after(350, self.capture)          # let the tray menu close first
                elif cmd == "open_folder":
                    os.makedirs(self.cfg["save_dir"], exist_ok=True)
                    os.startfile(self.cfg["save_dir"])
                elif cmd == "options":
                    self.open_options()
                elif cmd == "toggle_autostart":
                    set_autostart(not autostart_enabled())
                elif cmd == "quit":
                    return self.quit()
        except queue.Empty:
            pass
        except Exception:
            log.exception("poll error")
        self.root.after(80, self.poll)

    def capture(self):
        if self.overlay or self.options:
            return
        try:
            shot = ImageGrab.grab(all_screens=True)
            vx, vy, vw, vh = virtual_screen()
            if shot.size != (vw, vh):
                log.warning("grab size %s != virtual screen %s", shot.size, (vw, vh))
            self.overlay = CaptureOverlay(self, shot, (vx, vy))
        except Exception:
            log.exception("capture failed")
            self.overlay = None
            self.notify("Capture failed - see log")

    def quit(self):
        self.hk.stop()
        if self.icon:
            self.icon.stop()
        self.root.quit()

    def run(self):
        self.root.mainloop()


# ----------------------------------------------------------------------------- self-test / entry

def selftest():
    out = os.path.join(os.environ.get("TEMP", "."), "snapframe_selftest")
    os.makedirs(out, exist_ok=True)
    base = Image.new("RGB", (400, 240), (240, 240, 240))
    items = [
        {"tool": "pen", "pts": [(20, 20), (60, 50), (100, 30), (140, 70)], "color": "#e53935", "w": 3},
        {"tool": "line", "pts": [(20, 200), (200, 200)], "color": "#000000", "w": 2},
        {"tool": "arrow", "pts": [(220, 40), (360, 120)], "color": "#1e88e5", "w": 3},
        {"tool": "rect", "pts": [(30, 90), (150, 160)], "color": "#43a047", "w": 3},
        {"tool": "ellipse", "pts": [(180, 140), (300, 210)], "color": "#8e24aa", "w": 3},
        {"tool": "marker", "pts": [(20, 120), (180, 125)], "color": "#fdd835", "w": 3},
        {"tool": "text", "pts": [(220, 20)], "text": "SnapFrame 测试 Test", "color": "#e53935", "w": 3, "size": 19},
    ]
    img1, img2 = render_annotations(base, items, 0, 0, 1), render_annotations(base, items, 0, 0, 2)
    img2.save(os.path.join(out, "render.png"))
    assert img1.size == img2.size == (400, 240)
    assert img2.getpixel((60, 125)) != (240, 240, 240), "marker did not draw"
    assert img2.getpixel((90, 91)) != (240, 240, 240), "rect did not draw"
    copy_image_to_clipboard(img2)
    back = ImageGrab.grabclipboard()
    assert isinstance(back, Image.Image) and back.size == (400, 240), f"clipboard roundtrip failed: {back!r}"
    assert parse_hotkey("Ctrl+Alt+A") == (0x3, 0x41) and parse_hotkey("PrintScreen") == (0, 0x2C)
    vx, vy, vw, vh = virtual_screen()
    shot = ImageGrab.grab(all_screens=True)
    print(f"virtual screen {vw}x{vh} @ ({vx},{vy}); grab size {shot.size}")
    assert shot.size == (vw, vh), "grab size does not match virtual screen"
    print("selftest OK ->", out)


def single_instance():
    h = kernel32.CreateMutexW(None, False, f"Local\\{APP}_SingleInstance")
    return ctypes.GetLastError() != 183, h        # 183 = ERROR_ALREADY_EXISTS


def main():
    setup_logging()
    set_dpi_aware()
    if "--selftest" in sys.argv:
        return selftest()
    ok, _mutex = single_instance()
    if not ok:
        log.info("another instance is already running; exiting")
        return
    try:
        App(capture_now="--capture-now" in sys.argv).run()
    except Exception:
        log.exception("fatal")
        raise


if __name__ == "__main__":
    main()
