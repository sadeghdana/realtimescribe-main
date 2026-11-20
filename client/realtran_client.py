"""RealTran desktop client.

Runs on the PC where you attend the meeting. It captures that PC's microphone (you / people in
the room) and its system audio (remote participants in Teams / Zoom / ...), streams both to the
RealTran GPU server, and shows the translated subtitles in an always-on-top overlay at the
bottom of the screen.

    python realtran_client.py --ssh "user@server"      # tunnel through SSH (recommended)
    python realtran_client.py --server wss://192.168.1.10:8765   # direct over the LAN

Overlay: drag it with the left mouse button, right-click it for settings (languages, background
opacity, size, sources); the mouse wheel over it changes the background opacity.
"""
import argparse
import asyncio
import json
import os
import platform
import queue
import socket
import ssl
import subprocess
import sys
import threading
import time
import tkinter as tk
import urllib.parse
import webbrowser
import warnings
from collections import deque
from pathlib import Path

import numpy as np

CLIENT_VERSION = "2026.10.23-12"
SR = 16000
BLOCK = 512  # 32 ms
SETTINGS_PATH = Path(__file__).with_name("client_settings.json")
DEFAULTS = {"ui": "ja", "langs": ["ja"], "original": True, "lines": 3, "font": 22, "width": 0.7, "bg_opacity": 0.7, "original_small": False,
            "mic": True, "system": True, "x": None, "bottom": None}
# Interface strings (the subtitles themselves come from the server).
UI = {
    "ja": {
        "names": {"zh": "中国語", "ja": "日本語", "en": "英語"},
        "connected": "● 接続中", "disconnected": "○ 未接続",
        "mic": "マイク", "system": "システム音声", "off": "オフ", "unavailable": "使用不可",
        "tag": {"mic": "ローカル", "system": "オンライン"},
        "waiting": "発言を待っています…   ドラッグで移動・右クリックで設定（言語・透明度・文字サイズ）",
        "connecting": "サーバーに接続しています…",
        "version": "RealTran クライアント {v}", "show": "{lang}を表示", "original": "原文も表示",
        "original_small": "原文を小さく表示", "opacity": "背景の透明度（クリック後ホイールでも調整可）",
        "opaque": "不透明", "clear": "完全に透明（文字のみ）", "lines": "表示する文の数", "n_lines": "{n} 文",
        "font_up": "文字を大きく", "font_down": "文字を小さく", "wider": "幅を広げる", "narrower": "幅を狭める",
        "cap_mic": "収音：このPCのマイク", "cap_system": "収音：システム音声（オンライン参加者）",
        "reset": "画面の下に戻す", "quit": "終了",
        "rec_start": "⏺ 議事録の記録を開始", "rec_stop": "⏹ 記録を終了して議事録を作成",
        "open_minutes": "議事録を開く（ブラウザ）", "recording": "⏺ 記録中",
        "ssh_tip": "SSH を使わずに接続するには {script} で起動してください。",
        "summarizing": "議事録を作成中 {d}/{t}", "minutes_ready": "議事録ができました（右クリック →「議事録を開く」）",
    },
    "zh": {
        "names": {"zh": "中文", "ja": "日语", "en": "英语"},
        "connected": "● 已连接", "disconnected": "○ 未连接",
        "mic": "麦克风", "system": "系统声音", "off": "关", "unavailable": "不可用",
        "tag": {"mic": "本地", "system": "线上"},
        "waiting": "等待发言…   拖动可移动，右键设置语言/透明度/字号",
        "connecting": "正在连接服务器…",
        "version": "RealTran 客户端 {v}", "show": "显示{lang}", "original": "同时显示原文",
        "original_small": "原文用小字", "opacity": "背景透明度（点一下后也可用滚轮调）",
        "opaque": "不透明", "clear": "全透明（只显示文字）", "lines": "保留句数", "n_lines": "{n} 句",
        "font_up": "字号 +", "font_down": "字号 −", "wider": "宽度 +", "narrower": "宽度 −",
        "cap_mic": "收音：本机麦克风", "cap_system": "收音：系统声音（线上成员）",
        "reset": "复位到屏幕底部", "quit": "退出",
        "rec_start": "⏺ 开始记录议事录", "rec_stop": "⏹ 结束记录并生成议事录",
        "open_minutes": "打开议事录（浏览器）", "recording": "⏺ 记录中",
        "ssh_tip": "不使用 SSH 的话，请改用 {script} 启动。",
        "summarizing": "正在生成议事录 {d}/{t}", "minutes_ready": "议事录已生成（右键 →「打开议事录」）",
    },
    "en": {
        "names": {"zh": "Chinese", "ja": "Japanese", "en": "English"},
        "connected": "● Connected", "disconnected": "○ Offline",
        "mic": "Mic", "system": "System audio", "off": "off", "unavailable": "unavailable",
        "tag": {"mic": "Local", "system": "Online"},
        "waiting": "Waiting for speech…   Drag to move, right-click for language / opacity / size",
        "connecting": "Connecting to the server…",
        "version": "RealTran client {v}", "show": "Show {lang}", "original": "Also show the original",
        "original_small": "Smaller original text", "opacity": "Background opacity (or click, then mouse wheel)",
        "opaque": "Opaque", "clear": "Fully transparent (text only)", "lines": "Sentences shown", "n_lines": "{n}",
        "font_up": "Larger text", "font_down": "Smaller text", "wider": "Wider", "narrower": "Narrower",
        "cap_mic": "Capture: this PC's microphone", "cap_system": "Capture: system audio (remote participants)",
        "reset": "Back to the bottom of the screen", "quit": "Quit",
        "rec_start": "⏺ Start recording the meeting", "rec_stop": "⏹ Stop recording and write the minutes",
        "open_minutes": "Open the minutes (browser)", "recording": "⏺ Recording",
        "ssh_tip": "To connect without SSH, start with {script} instead.",
        "summarizing": "Writing minutes {d}/{t}", "minutes_ready": "Minutes are ready (right-click → Open the minutes)",
    },
}
UI_LANGS = {"ja": "日本語", "zh": "中文", "en": "English"}
DEBUG = bool(os.environ.get("REALTRAN_DEBUG"))


def debug(*args):
    if DEBUG:
        print("[overlay]", *args, flush=True)
SYSTEM = platform.system()
LAN_LAUNCHER = "start_client_lan.bat" if SYSTEM == "Windows" else "start_client_lan.sh"
FONTS = {
    "Windows": {"zh": "Microsoft YaHei UI", "ja": "Meiryo", "en": "Segoe UI"},
    "Darwin": {"zh": "PingFang SC", "ja": "Hiragino Sans", "en": "Helvetica Neue"},
}.get(SYSTEM, {"zh": "Noto Sans CJK SC", "ja": "Noto Sans CJK JP", "en": "DejaVu Sans"})
# Size compensation so Chinese and Japanese look equally big: at the same point size Meiryo's
# kanji are ~7% smaller than Microsoft YaHei's (measured ink height 87.5 vs 93.2).
FONT_SCALE = {"Windows": {"ja": 1.07}}.get(SYSTEM, {})


def load_settings():
    s = dict(DEFAULTS)
    try:
        s.update(json.loads(SETTINGS_PATH.read_text(encoding="utf-8")))
    except (OSError, ValueError):
        pass
    return s


def save_settings(s):
    try:
        SETTINGS_PATH.write_text(json.dumps(s, ensure_ascii=False, indent=1), encoding="utf-8")
    except OSError:
        pass


# ---------------------------------------------------------------------------------------------
# Audio capture
# ---------------------------------------------------------------------------------------------
def deliver(kind, x, state, send, meter):
    """Level meter, ducking and Int16 conversion shared by every capture path."""
    rms = float(np.sqrt(np.mean(x * x))) if x.size else 0.0
    meter.level = rms
    if not state.settings[kind]:
        return
    if kind == "system" and rms > 0.004:  # about -48 dBFS
        state.system_voice_at = time.monotonic()
    if kind == "mic" and state.duck and time.monotonic() - state.system_voice_at < 0.5:
        # Remote participants are talking: their voice also reaches the mic through the
        # speakers, so mute the mic to avoid duplicate subtitles.
        x = np.zeros_like(x)
    send(kind, (np.clip(x, -1, 1) * 32767).astype("<i2").tobytes())


class Capture(threading.Thread):
    """Records one input device at 16 kHz and hands Int16 frames to `send`.

    kind="mic": the default microphone (or --mic-device).
    kind="system" with --system-device: a virtual input such as BlackHole (macOS).
    """

    def __init__(self, kind, device_name, state, send):
        super().__init__(daemon=True)
        self.kind = kind
        self.device_name = device_name
        self.state = state
        self.send = send
        self.level = 0.0
        self.error = ""
        self.device = ""  # name of the device being recorded, shown in the menu

    def _open(self, sc):
        if self.device_name:
            return sc.get_microphone(self.device_name)
        return sc.default_microphone()

    def run(self):
        import soundcard as sc
        warnings.filterwarnings("ignore", category=sc.SoundcardRuntimeWarning)
        while True:
            try:
                dev = self._open(sc)
                if dev.name != self.device:
                    if self.device:
                        print(f"[{self.kind}] switched to: {dev.name}", flush=True)
                    self.device = dev.name
                self.error = ""
                # Without --mic-device, follow the system default: a headset plugged in while the
                # client runs is picked up within a few seconds.
                follow = self.kind == "mic" and not self.device_name
                next_check = time.monotonic() + 3
                with dev.recorder(samplerate=SR, channels=1, blocksize=BLOCK) as rec:
                    while True:
                        x = rec.record(numframes=BLOCK)[:, 0].astype(np.float32)
                        deliver(self.kind, x, self.state, self.send, self)
                        if follow and time.monotonic() >= next_check:
                            next_check = time.monotonic() + 3
                            if sc.default_microphone().id != dev.id:
                                break  # reopen on the new default device
            except Exception as e:  # device unplugged / none available: retry
                self.error = str(e) or type(e).__name__
                self.level = 0.0
                time.sleep(3)


class Unavailable:
    """A capture source that cannot exist on this machine (shown as "unavailable")."""

    def __init__(self, reason):
        self.level = 0.0
        self.error = reason

    def start(self):
        print(f"[system audio] {self.error}", flush=True)


class SystemAudio(threading.Thread):
    """What this PC plays: loopback of *every* output device, mixed into one stream.

    Recording only the default speaker misses audio that apps send elsewhere — a browser playing
    YouTube on the laptop speakers while Teams uses a headset, or a headset plugged in after the
    client started. Devices are rescanned every few seconds, so plugging and unplugging is
    followed automatically.
    """

    kind = "system"

    def __init__(self, state, send):
        super().__init__(daemon=True)
        self.state = state
        self.send = send
        self.level = 0.0
        self.error = ""
        self.devices = {}   # speaker id -> name, for the readers that are running
        self._bufs = {}     # speaker id -> deque of 32 ms blocks
        self._lock = threading.Lock()

    def run(self):
        import soundcard as sc
        warnings.filterwarnings("ignore", category=sc.SoundcardRuntimeWarning)
        threading.Thread(target=self._mix, daemon=True).start()
        while True:
            try:
                for spk in sc.all_speakers():
                    if spk.id not in self.devices:
                        self.devices[spk.id] = spk.name
                        debug("loopback start:", spk.name)
                        threading.Thread(target=self._read, args=(sc, spk), daemon=True).start()
                self.error = "" if self.devices else "no output device"
            except Exception as e:
                self.error = str(e) or type(e).__name__
            time.sleep(3)

    def _read(self, sc, spk):
        try:
            dev = sc.get_microphone(id=str(spk.id), include_loopback=True)
            with dev.recorder(samplerate=SR, channels=1, blocksize=BLOCK) as rec:
                while True:
                    x = rec.record(numframes=BLOCK)[:, 0].astype(np.float32)
                    with self._lock:
                        q = self._bufs.setdefault(spk.id, deque())
                        q.append(x)
                        while len(q) > 8:  # this device runs ahead of the mixer: drop the oldest
                            q.popleft()
        except Exception as e:  # unplugged / disabled: the next scan restarts it if it is back
            print(f"[system audio] {spk.name}: {type(e).__name__}: {e}", flush=True)
        finally:
            with self._lock:
                self._bufs.pop(spk.id, None)
            self.devices.pop(spk.id, None)

    def _mix(self):
        # One 32 ms block per tick from every device that has one; silent devices simply add nothing.
        period = BLOCK / SR
        next_t = time.perf_counter()
        while True:
            next_t += period
            wait = next_t - time.perf_counter()
            if wait > 0:
                time.sleep(wait)
            elif wait < -0.5:  # fell far behind (machine was busy): resynchronise
                next_t = time.perf_counter()
            with self._lock:
                blocks = [q.popleft() for q in self._bufs.values() if q]
            x = np.sum(blocks, axis=0, dtype=np.float32) if blocks else np.zeros(BLOCK, np.float32)
            deliver("system", x, self.state, self.send, self)


def test_audio(seconds=5):
    """Record every output device (loopback) and every microphone at the same time and report
    what actually arrives, to find out where an application's sound is (or is not) going."""
    import soundcard as sc
    warnings.filterwarnings("ignore", category=sc.SoundcardRuntimeWarning)
    default_spk = sc.default_speaker().id
    try:
        default_mic = sc.default_microphone().id
    except Exception:
        default_mic = None
    targets = [(f"[speaker] {d.name}" + ("  (default)" if d.id == default_spk else ""), d.id, True)
               for d in sc.all_speakers()]
    targets += [(f"[mic] {d.name}" + ("  (default)" if d.id == default_mic else ""), d.id, False)
                for d in sc.all_microphones()]
    results = {}

    def measure(label, dev_id, loopback):
        try:
            dev = sc.get_microphone(id=str(dev_id), include_loopback=loopback)
            got, end = [], time.monotonic() + seconds
            with dev.recorder(samplerate=SR, channels=1, blocksize=BLOCK) as rec:
                while time.monotonic() < end:
                    got.append(rec.record(numframes=BLOCK)[:, 0])
            x = np.concatenate(got)
            results[label] = (len(x) / SR, float(np.sqrt(np.mean(x * x))), float(np.abs(x).max()))
        except Exception as e:
            results[label] = f"ERROR {type(e).__name__}: {e}"

    print(f"Now play something with speech (YouTube, a meeting...). Recording {len(targets)} devices "
          f"for {seconds} s...", flush=True)
    threads = [threading.Thread(target=measure, args=t, daemon=True) for t in targets]
    for t in threads:
        t.start()
    for t in threads:
        t.join(seconds + 3)
    for label, *_ in targets:
        r = results.get(label)
        if r is None:
            line = "no data at all (the device delivered nothing)"
        elif isinstance(r, str):
            line = r
        else:
            secs, rms, peak = r
            verdict = "SOUND " if rms > 0.004 else "silent"
            line = f"{verdict} level {20 * np.log10(rms + 1e-9):6.1f} dBFS, peak {peak:.3f}, {secs:.1f} s received"
        print(f"  {label}\n      {line}")


# ---------------------------------------------------------------------------------------------
# Networking (runs in its own asyncio thread)
# ---------------------------------------------------------------------------------------------
class Net(threading.Thread):
    def __init__(self, url, state):
        super().__init__(daemon=True)
        self.url = url.rstrip("/")
        self.state = state
        self.loop = None
        self.queues = {}
        self.ssl = None
        if self.url.startswith("wss://"):
            # The server uses a self-signed certificate on the LAN.
            self.ssl = ssl.create_default_context()
            self.ssl.check_hostname = False
            self.ssl.verify_mode = ssl.CERT_NONE

    @property
    def session(self):
        """This PC's session on the server: its microphone, system audio and subtitles."""
        return f"pc:{self.state.host}"

    @property
    def web_url(self):
        """This PC's session in the server's web UI, reached the same way as the subtitles."""
        base = self.url.replace("wss://", "https://").replace("ws://", "http://")
        return f"{base}/?session={urllib.parse.quote(self.session)}"

    def command(self, obj):
        """Send a control command (record start / stop ...) over the subtitle connection."""
        if self.loop is not None:
            self.loop.call_soon_threadsafe(self._cmds.put_nowait, obj)

    def send(self, kind, data):
        """Called from capture threads."""
        if self.loop is not None and kind in self.queues:
            self.loop.call_soon_threadsafe(self._enqueue, kind, data)

    def _enqueue(self, kind, data):
        q = self.queues[kind]
        if q.full():  # disconnected for a while: keep only the latest audio
            q.get_nowait()
        q.put_nowait(data)

    def run(self):
        self.loop = asyncio.new_event_loop()
        asyncio.set_event_loop(self.loop)
        self.queues = {k: asyncio.Queue(maxsize=60) for k in ("mic", "system")}
        self._cmds = asyncio.Queue()
        tasks = [self._subtitles()] + [self._audio(k) for k in ("mic", "system")]
        self.loop.run_until_complete(asyncio.gather(*tasks))

    async def _connect(self, path):
        import websockets
        kw = dict(ssl=self.ssl, max_size=None, open_timeout=5)
        try:  # never route these connections through a system / corporate HTTP proxy
            return await websockets.connect(self.url + path, proxy=None, **kw)
        except TypeError:  # websockets < 15 has no proxy support at all
            return await websockets.connect(self.url + path, **kw)

    async def _subtitles(self):
        while True:
            try:
                ws = await self._connect("/ws?session=" + urllib.parse.quote(self.session))
                async with ws:
                    self.state.ui.put({"type": "conn", "ok": True})
                    sender = asyncio.ensure_future(self._send_commands(ws))
                    try:
                        async for m in ws:
                            self.state.ui.put(json.loads(m))
                    finally:
                        sender.cancel()
            except Exception:
                pass
            self.state.ui.put({"type": "conn", "ok": False})
            await asyncio.sleep(2)

    async def _send_commands(self, ws):
        while True:
            await ws.send(json.dumps(await self._cmds.get()))

    async def _audio(self, kind):
        q = self.queues[kind]
        name = self.state.host
        while True:
            first = await q.get()  # only open the channel once this source actually has audio
            try:
                ws = await self._connect("/ws/audio")
                async with ws:
                    await ws.send(json.dumps({"sr": SR, "name": name, "client": self.state.host, "channel": kind}))
                    reply = json.loads(await ws.recv())
                    if reply.get("type") != "mic":
                        raise RuntimeError(reply.get("msg", "refused"))
                    while not q.empty():  # drop audio that queued up while connecting
                        first = q.get_nowait()
                    await ws.send(first)
                    while True:
                        await ws.send(await q.get())
            except Exception:
                pass
            await asyncio.sleep(2)


_JOBS = []
FATAL_SSH = ("Host key verification failed", "Permission denied", "Could not resolve hostname",
             "Connection refused", "Connection timed out", "No route to host")


def _free_port():
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


def _kill_with_us(proc):
    """Windows: tie a child process to this one, so it is killed when we exit in any way (even
    when the console window is closed). Otherwise a leftover ssh keeps holding its port."""
    if SYSTEM != "Windows":
        return
    import ctypes
    from ctypes import wintypes

    class IoCounters(ctypes.Structure):
        _fields_ = [(n, ctypes.c_ulonglong) for n in ("r_ops", "w_ops", "o_ops", "r_bytes", "w_bytes", "o_bytes")]

    class BasicLimits(ctypes.Structure):
        _fields_ = [("PerProcessUserTimeLimit", ctypes.c_int64), ("PerJobUserTimeLimit", ctypes.c_int64),
                    ("LimitFlags", wintypes.DWORD), ("MinimumWorkingSetSize", ctypes.c_size_t),
                    ("MaximumWorkingSetSize", ctypes.c_size_t), ("ActiveProcessLimit", wintypes.DWORD),
                    ("Affinity", ctypes.c_size_t), ("PriorityClass", wintypes.DWORD),
                    ("SchedulingClass", wintypes.DWORD)]

    class ExtendedLimits(ctypes.Structure):
        _fields_ = [("BasicLimitInformation", BasicLimits), ("IoInfo", IoCounters),
                    ("ProcessMemoryLimit", ctypes.c_size_t), ("JobMemoryLimit", ctypes.c_size_t),
                    ("PeakProcessMemoryUsed", ctypes.c_size_t), ("PeakJobMemoryUsed", ctypes.c_size_t)]

    try:
        k32 = ctypes.windll.kernel32
        k32.CreateJobObjectW.restype = wintypes.HANDLE
        k32.SetInformationJobObject.argtypes = (wintypes.HANDLE, ctypes.c_int, ctypes.c_void_p, wintypes.DWORD)
        k32.AssignProcessToJobObject.argtypes = (wintypes.HANDLE, wintypes.HANDLE)
        job = k32.CreateJobObjectW(None, None)
        info = ExtendedLimits()
        info.BasicLimitInformation.LimitFlags = 0x2000  # JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
        k32.SetInformationJobObject(job, 9, ctypes.byref(info), ctypes.sizeof(info))
        k32.AssignProcessToJobObject(job, int(proc._handle))
        _JOBS.append(job)  # the handle stays open until this process ends
    except Exception as e:
        debug("job object:", e)


class SshTunnel(threading.Thread):
    """Keeps `ssh -N -L <local>:127.0.0.1:<remote>` running (relies on key-based login).

    The local port is a free one picked at start-up, so a tunnel left over from an earlier run
    can never be mistaken for ours.
    """

    def __init__(self, target, remote_port):
        super().__init__(daemon=True)
        self.local_port = _free_port()
        self.cmd = ["ssh", "-N", "-o", "ExitOnForwardFailure=yes", "-o", "ServerAliveInterval=15",
                    "-o", "ServerAliveCountMax=3", "-o", "BatchMode=yes",
                    "-L", f"{self.local_port}:127.0.0.1:{remote_port}", target]
        self.proc = None
        self.last_error = ""
        self.tip_shown = False

    def run(self):
        while True:
            try:
                self.proc = subprocess.Popen(self.cmd, stdin=subprocess.DEVNULL, stderr=subprocess.PIPE,
                                             text=True, errors="replace")
            except OSError as e:  # no ssh client installed
                self.last_error = str(e)
                print("ssh:", e, flush=True)
                time.sleep(10)
                continue
            _kill_with_us(self.proc)
            for line in self.proc.stderr:  # ssh only writes here when something goes wrong
                line = line.strip()
                if line:
                    self.last_error = line
                    print("ssh:", line, flush=True)
                    if not self.tip_shown and any(k in line for k in FATAL_SSH):
                        self.tip_shown = True
                        print(f"  -> This PC cannot log in to the server over SSH. Use {LAN_LAUNCHER} "
                              "instead (it connects over the LAN, no SSH needed).", flush=True)
            self.proc.wait()
            time.sleep(3)  # dropped (network change, server reboot...): reconnect

    def wait_ready(self, timeout=20):
        end = time.time() + timeout
        while time.time() < end:
            try:
                with socket.create_connection(("127.0.0.1", self.local_port), timeout=1):
                    return True
            except OSError:
                time.sleep(0.3)
        return False

    def stop(self):
        if self.proc and self.proc.poll() is None:
            self.proc.terminate()


# ---------------------------------------------------------------------------------------------
# Overlay window
# ---------------------------------------------------------------------------------------------
class State:
    def __init__(self, settings, duck):
        self.tunnel = None
        self.settings = settings
        self.duck = duck
        self.system_voice_at = 0.0
        self.host = socket.gethostname()
        self.ui: "queue.Queue[dict]" = queue.Queue()


def _win_exstyle(win, add):
    """Add extended window styles to a Tk toplevel (Windows only)."""
    import ctypes
    hwnd = ctypes.windll.user32.GetParent(win.winfo_id())
    style = ctypes.windll.user32.GetWindowLongW(hwnd, -20)  # GWL_EXSTYLE
    ctypes.windll.user32.SetWindowLongW(hwnd, -20, style | add)
    return hwnd


class Overlay:
    """Subtitle overlay.

    Drawn on a single Canvas and redrawn in place (no widgets are created or destroyed), which
    is what made the first version flicker. Each sentence keeps the same layout for its whole
    life — the translation line is reserved with "…" until it arrives — so new text never pushes
    the lines above it around, and the window only shrinks after the content has stayed smaller
    for a few seconds.

    On Windows the overlay is two windows: a dark box whose opacity is adjustable, and on top of
    it a window that shows only the text (everything else is see-through and click-through), so
    the text stays crisp at any background opacity.
    """

    BG = "#101215"
    KEY = "#010203"      # colour made fully transparent in the text window (Windows)
    FG = "#f4f6f8"
    OLD = "#c3cad3"
    DIM = "#8b94a1"
    LIVE = "#a9b2bd"
    OUTLINE = "#000000"
    TAG = {"system": "#5fb3ff", "mic": "#7fd18b"}
    PADX, PADY, TAGW = 16, 8, 46
    OPACITY_STEPS = [1.0, 0.85, 0.7, 0.55, 0.4, 0.25, 0.0]

    def __init__(self, state, captures, net=None):
        self.net = net
        self.meeting = {}
        self.notice = ("", 0.0)
        self.state = state
        self.s = state.settings
        self.captures = captures
        self.segments = {}   # id -> segment (with "tr")
        self.order = []
        self.live = {}       # channel -> partial
        self.connected = False
        self.dirty = True
        self.last_render = 0.0
        self.height = 0
        self.shrink_since = None
        self.fonts = {}
        self.layered = SYSTEM == "Windows"
        self.tag_w = self.TAGW

        self.root = tk.Tk()
        self.root.title("RealTran")
        self.root.overrideredirect(True)
        self.root.attributes("-topmost", True)
        canvas_bg = self.KEY if self.layered else self.BG
        self.root.configure(bg=canvas_bg)
        self.canvas = tk.Canvas(self.root, bg=canvas_bg, highlightthickness=0, bd=0)
        self.canvas.pack(fill="both", expand=True)
        self.backdrop = None
        if self.layered:
            self.root.attributes("-transparentcolor", self.KEY)
            self.backdrop = tk.Toplevel(self.root)
            self.backdrop.overrideredirect(True)
            self.backdrop.attributes("-topmost", True)
            self.backdrop.configure(bg=self.BG)
            self.root.after(100, self._init_layers)

        # The whole box reacts to the mouse: on Windows the see-through parts of the text window
        # pass clicks down to the box window below it, so both windows get the same bindings.
        for w in (self.canvas, self.backdrop):
            if w is None:
                continue
            w.bind("<ButtonPress-1>", self._drag_start)
            w.bind("<B1-Motion>", self._drag_move)
            w.bind("<ButtonRelease-1>", self._drag_end)
            w.bind("<Button-3>", self._popup)
            w.bind("<MouseWheel>", self._wheel)
            if SYSTEM == "Darwin":
                w.bind("<Button-2>", self._popup)
        self.menu = self._build_menu()
        if DEBUG:
            self.root.bind_all("<Button>", lambda e: debug("button", e.num, e.widget, e.x_root, e.y_root), add="+")
        self._apply_opacity()
        self.root.after(50, self._poll)
        self.root.after(250, self._tick_status)

    # ---- layers / opacity -------------------------------------------------------------------
    def _init_layers(self):
        # Tk creates the real Windows windows only when it first shows them. On a slow PC that
        # happens after this first call, and the owner set below would then not take effect:
        # the box ended up on top of the text, hiding it more the more opaque it was.
        if not (self.root.winfo_ismapped() and self.backdrop.winfo_ismapped()):
            self.root.after(50, self._init_layers)
            return
        # The dark box takes clicks (drag / right-click) and focus (Tk sends wheel events to the
        # focused window, and context menus need the foreground window).
        WS_EX_LAYERED, WS_EX_TOOLWINDOW = 0x80000, 0x80
        box = _win_exstyle(self.backdrop, WS_EX_LAYERED | WS_EX_TOOLWINDOW)
        self.root_hwnd = _win_exstyle(self.root, WS_EX_TOOLWINDOW)
        # Make the text window an "owned" window of the box: Windows always keeps owned windows
        # above their owner, so the box can never end up covering the text.
        import ctypes
        set_owner = ctypes.windll.user32.SetWindowLongPtrW
        set_owner.argtypes = (ctypes.c_void_p, ctypes.c_int, ctypes.c_void_p)
        set_owner(self.root_hwnd, -8, box)  # GWLP_HWNDPARENT
        self.box_hwnd = box
        self._apply_opacity()
        self._place(*self._size)

    def _check_layers(self):
        """Put the text window back on the box if the two ever got separated."""
        if not hasattr(self, "root_hwnd"):
            return
        import ctypes
        u32 = ctypes.windll.user32
        box = u32.GetParent(self.backdrop.winfo_id())
        text = u32.GetParent(self.root.winfo_id())
        if (box, text) != (self.box_hwnd, self.root_hwnd) or u32.GetWindow(text, 4) != box:  # GW_OWNER
            debug("layers separated: re-attaching the text window to the box")
            self._init_layers()
        self._raise_text()

    def _apply_opacity(self):
        op = float(self.s["bg_opacity"])
        if self.layered:
            # Never fully 0: an invisible 2% box still catches the mouse, so the overlay can be
            # dragged and right-clicked even in text-only mode.
            self.backdrop.attributes("-alpha", max(0.02, op))
            self._raise_text()
        else:  # no per-layer transparency: fade the whole window, but keep text readable
            self.root.attributes("-alpha", max(0.35, op))
        self.dirty = True

    def _raise_text(self):
        """If the box is above the text window, move the text window back on top."""
        if not (self.layered and hasattr(self, "root_hwnd")):
            return
        import ctypes
        u32 = ctypes.windll.user32
        h = self.box_hwnd
        while h:  # walk up from the box: normally the text window is right above it
            h = u32.GetWindow(h, 3)  # GW_HWNDPREV
            if h == self.root_hwnd:
                return
        # Pointer-sized arguments: as a plain int, HWND_TOPMOST (-1) does not arrive as -1 on
        # 64-bit Windows and the call silently does nothing.
        set_pos = u32.SetWindowPos
        set_pos.argtypes = (ctypes.c_void_p, ctypes.c_void_p) + (ctypes.c_int,) * 4 + (ctypes.c_uint,)
        set_pos(self.root_hwnd, -1, 0, 0, 0, 0, 0x2 | 0x1 | 0x10)  # HWND_TOPMOST, NOMOVE|NOSIZE|NOACTIVATE

    def _set_opacity(self, op):
        self.s["bg_opacity"] = round(min(1.0, max(0.0, op)), 2)
        save_settings(self.s)
        self._apply_opacity()

    def _wheel(self, e):
        debug("wheel", e.delta)
        self._set_opacity(self.s["bg_opacity"] + (0.1 if e.delta > 0 else -0.1))

    # ---- window placement / dragging --------------------------------------------------------
    def _drag_start(self, e):
        debug("press", e.widget, e.x_root, e.y_root)
        self._drag = (e.x_root - self.root.winfo_x(), e.y_root - self.root.winfo_y())

    def _drag_move(self, e):
        if getattr(self, "_drag", None) is None:
            return
        dx, dy = self._drag
        x, y = e.x_root - dx, e.y_root - dy
        w, h = self._size
        # Move the anchor along with the window, so a redraw in the middle of the drag does not
        # snap it back to where it was.
        self.s["x"], self.s["bottom"] = x + w // 2, y + h
        self.root.geometry(f"+{x}+{y}")
        if self.backdrop is not None:
            self.backdrop.geometry(f"+{x}+{y}")

    def _drag_end(self, e):
        debug("release", e.x_root, e.y_root)
        if getattr(self, "_drag", None) is not None:
            self._drag = None
            save_settings(self.s)

    def _place(self, w, h):
        self._size = (w, h)
        sw, sh = self.root.winfo_screenwidth(), self.root.winfo_screenheight()
        cx = self.s["x"] if self.s["x"] is not None else sw // 2
        bottom = self.s["bottom"] if self.s["bottom"] is not None else sh - 80
        x = max(0, min(sw - w, cx - w // 2))
        y = max(0, bottom - h)
        geo = f"{w}x{h}+{x}+{y}"
        if self.root.geometry() != geo:
            self.root.geometry(geo)
            if self.backdrop is not None:
                self.backdrop.geometry(geo)
                self._raise_text()

    # ---- settings menu ----------------------------------------------------------------------
    @property
    def t(self):
        return UI.get(self.s.get("ui"), UI["ja"])

    def _build_menu(self):
        t = self.t
        m = tk.Menu(self.root, tearoff=0)
        m.add_command(label=t["version"].format(v=CLIENT_VERSION), state="disabled")
        m.add_separator()
        self.lang_vars = {}
        for code, label in t["names"].items():
            v = tk.BooleanVar(value=code in self.s["langs"])
            self.lang_vars[code] = v
            m.add_checkbutton(label=t["show"].format(lang=label), variable=v, command=self._langs_changed)
        self.orig_var = tk.BooleanVar(value=self.s["original"])
        m.add_checkbutton(label=t["original"], variable=self.orig_var, command=self._toggle("original", self.orig_var))
        self.small_var = tk.BooleanVar(value=self.s["original_small"])
        m.add_checkbutton(label=t["original_small"], variable=self.small_var,
                          command=self._toggle("original_small", self.small_var))
        m.add_separator()
        opacity = tk.Menu(m, tearoff=0)
        self.op_var = tk.DoubleVar(value=self.s["bg_opacity"])
        for op in self.OPACITY_STEPS:
            label = t["opaque"] if op == 1.0 else (t["clear"] if op == 0 else f"{int(op * 100)}%")
            opacity.add_radiobutton(label=label, variable=self.op_var, value=op,
                                    command=lambda: self._set_opacity(self.op_var.get()))
        m.add_cascade(label=t["opacity"], menu=opacity)
        lines = tk.Menu(m, tearoff=0)
        self.lines_var = tk.IntVar(value=self.s["lines"])
        for n in range(1, 6):
            lines.add_radiobutton(label=t["n_lines"].format(n=n), variable=self.lines_var, value=n,
                                  command=lambda: self._set("lines", self.lines_var.get()))
        m.add_cascade(label=t["lines"], menu=lines)
        m.add_command(label=t["font_up"], command=lambda: self._set("font", min(48, self.s["font"] + 2)))
        m.add_command(label=t["font_down"], command=lambda: self._set("font", max(12, self.s["font"] - 2)))
        m.add_command(label=t["wider"], command=lambda: self._set("width", min(1.0, round(self.s["width"] + 0.1, 1))))
        m.add_command(label=t["narrower"], command=lambda: self._set("width", max(0.3, round(self.s["width"] - 0.1, 1))))
        m.add_separator()
        self.mic_var = tk.BooleanVar(value=self.s["mic"])
        self.sys_var = tk.BooleanVar(value=self.s["system"])
        mic_name = getattr(self.captures.get("mic"), "device", "")
        m.add_checkbutton(label=t["cap_mic"] + (f"（{mic_name}）" if mic_name else ""), variable=self.mic_var,
                          command=self._toggle("mic", self.mic_var))
        m.add_checkbutton(label=t["cap_system"], variable=self.sys_var, command=self._toggle("system", self.sys_var))
        if self.net is not None:
            m.add_separator()
            rec = bool(self.meeting.get("recording"))
            m.add_command(label=t["rec_stop"] if rec else t["rec_start"], command=self._toggle_recording,
                          state="normal" if self.connected else "disabled")
            m.add_command(label=t["open_minutes"], command=lambda: webbrowser.open(self.net.web_url + "#minutes"),
                          state="normal" if self.connected else "disabled")
        m.add_separator()
        ui = tk.Menu(m, tearoff=0)
        self.ui_var = tk.StringVar(value=self.s.get("ui", "ja"))
        for code, label in UI_LANGS.items():
            ui.add_radiobutton(label=label, variable=self.ui_var, value=code, command=self._ui_changed)
        m.add_cascade(label="表示言語 / 界面语言 / Language", menu=ui)
        m.add_separator()
        m.add_command(label=t["reset"], command=self._reset_position)
        m.add_command(label=t["quit"], command=self.root.destroy)
        return m

    def _toggle_recording(self):
        if self.meeting.get("recording"):
            self.net.command({"cmd": "meeting_stop", "lang": self.s.get("ui", "ja")})
        else:
            self.net.command({"cmd": "meeting_start"})

    def _ui_changed(self):
        self.s["ui"] = self.ui_var.get()
        save_settings(self.s)
        self.menu = self._build_menu()
        self.height = 0
        self.dirty = True

    def _popup(self, e):
        debug("menu", e.widget, e.x_root, e.y_root)
        self.menu.destroy()
        self.menu = self._build_menu()
        self.op_var.set(self.s["bg_opacity"])
        # Windows only shows a context menu properly for the foreground window.
        e.widget.winfo_toplevel().focus_force()
        try:
            self.menu.tk_popup(e.x_root, e.y_root)
        finally:
            self.menu.grab_release()

    def _langs_changed(self):
        langs = [c for c, v in self.lang_vars.items() if v.get()]
        if not langs:  # keep at least one
            self.lang_vars[self.s["langs"][0]].set(True)
            return
        self._set("langs", langs)

    def _toggle(self, key, var):
        return lambda: self._set(key, bool(var.get()))

    def _set(self, key, value):
        self.s[key] = value
        save_settings(self.s)
        self.height = 0  # layout changed: fit the window to it right away
        self.dirty = True

    def _reset_position(self):
        self.s["x"] = None
        self._set("bottom", None)

    # ---- incoming events --------------------------------------------------------------------
    def _poll(self):
        try:
            while True:
                self._handle(self.state.ui.get_nowait())
        except queue.Empty:
            pass
        now = time.monotonic()
        if self.shrink_since is not None and now - self.shrink_since > 4:
            self.dirty = True
        if self.dirty and now - self.last_render >= 0.1:  # at most 10 redraws per second
            self.dirty = False
            self.last_render = now
            self._render()
        self.root.after(50, self._poll)

    def _handle(self, m):
        t = m.get("type")
        if t == "conn":
            if self.connected == m["ok"]:
                return
            self.connected = m["ok"]
            if not m["ok"]:
                self.live.clear()
        elif t == "status":
            self.meeting = m.get("meeting") or {}
            return
        elif t == "minutes":
            if m.get("ok"):
                self.notice = (self.t["minutes_ready"], time.monotonic() + 30)
            return
        elif t == "hello":
            self.meeting = (m.get("status") or {}).get("meeting") or {}
            self.segments = {s["id"]: s for s in m["segments"]}
            self.order = [s["id"] for s in m["segments"]]
            self.live.clear()
        elif t == "partial":
            old = self.live.get(m["ch"])
            if old and old["id"] == m["id"] and old["text"] == m["text"] and old["lang"] == m["lang"]:
                return
            self.live[m["ch"]] = m
        elif t == "final":
            self.segments[m["id"]] = {**m, "tr": {}}
            self.order.append(m["id"])
            if self.live.get(m["ch"], {}).get("id") == m["id"]:
                del self.live[m["ch"]]
        elif t == "translation" and m["id"] in self.segments:
            self.segments[m["id"]]["tr"] = m["tr"]
        elif t == "drop":
            if self.live.get(m.get("ch"), {}).get("id") == m["id"]:
                del self.live[m["ch"]]
        else:
            return
        if len(self.order) > 200:
            for i in self.order[:-100]:
                self.segments.pop(i, None)
            self.order = self.order[-100:]
        self.dirty = True

    # ---- drawing ----------------------------------------------------------------------------
    def _font(self, lang, size, bold=False):
        key = (lang, size, bold)
        if key not in self.fonts:
            from tkinter import font as tkfont
            size = int(round(size * FONT_SCALE.get(lang, 1.0)))
            self.fonts[key] = tkfont.Font(family=FONTS.get(lang, FONTS["zh"]), size=size,
                                          weight="bold" if bold else "normal")
        return self.fonts[key]

    def _kind(self, item):
        return "system" if str(item.get("ch", "")).endswith(":system") else "mic"

    def _lines_for(self, item, live, newest):
        """Lines of one sentence. Its shape does not change between live / final / translated."""
        fs = self.s["font"]
        langs = self.s["langs"]
        src = item.get("lang") or ""
        lines = []
        for lg in langs:
            if src == lg:
                color = self.LIVE if live else (self.FG if newest else self.OLD)
                lines.append((item["text"], color, fs, lg))
            else:
                tr = None if live else item.get("tr", {}).get(lg)
                lines.append((tr, self.FG if newest else self.OLD, fs, lg) if tr else ("…", self.DIM, fs, lg))
        if src not in langs and (self.s["original"] or live or any(t == "…" for t, *_ in lines)):
            size = int(fs * 0.75) if self.s["original_small"] else fs  # same size as the translation by default
            lines.append((item["text"], self.LIVE if live else self.DIM, max(10, size), src or "en"))
        return lines

    def _draw_text(self, x, y, text, color, font, width, tags="cap"):
        r = self.outline  # outline radius in pixels (0 = none)
        if r:
            for dx, dy in ((-r, 0), (r, 0), (0, -r), (0, r), (-1, -1), (1, 1), (-1, 1), (1, -1)):
                self.canvas.create_text(x + dx, y + dy, text=text, fill=self.OUTLINE, font=font, width=width,
                                        anchor="nw", tags=tags)
        return self.canvas.create_text(x, y, text=text, fill=color, font=font, width=width, anchor="nw", tags=tags)

    def _layout(self, blocks, wrap):
        c = self.canvas
        c.delete("all")
        y = self.PADY
        status = self._draw_text(self.PADX, y, self._status_text(), self._status_color(),
                                 self._font(self.s["ui"], 9), wrap + self.tag_w, tags="status")
        self.status_item = status
        y = c.bbox(status)[3] + 6
        if not blocks:
            hint = self.t["waiting"] if self.connected else self.t["connecting"]
            tunnel = self.state.tunnel
            if not self.connected and tunnel is not None and tunnel.last_error:
                hint += f"\nSSH: {tunnel.last_error}\n" + self.t["ssh_tip"].format(script=LAN_LAUNCHER)
            self._draw_text(self.PADX, y, hint, self.DIM, self._font(self.s["ui"], 12), wrap + self.tag_w)
            y = c.bbox("cap")[3]
        for n, (kind, lines) in enumerate(blocks):
            tag_y = y
            for text, color, size, lg in lines:
                item = self._draw_text(self.PADX + self.tag_w, y, text, color, self._font(lg, size), wrap)
                y = c.bbox(item)[3] + 2
            self._draw_text(self.PADX, tag_y + max(2, int(self.s["font"] * 0.35)), self.t["tag"][kind],
                            self.TAG[kind], self._font(self.s["ui"], 9, True), self.tag_w)
            y += 8
        return y + self.PADY

    def _render(self):
        # A thin dark outline keeps text readable once the box behind it is mostly see-through.
        op = self.s["bg_opacity"]
        self.outline = 0 if (not self.layered or op >= 0.4) else (2 if op < 0.15 else 1)
        sw, sh = self.root.winfo_screenwidth(), self.root.winfo_screenheight()
        w = int(sw * self.s["width"])
        # The source tag column is as wide as the longest tag in the current UI language.
        tag_font = self._font(self.s["ui"], 9, True)
        self.tag_w = max(self.TAGW, max(tag_font.measure(x) for x in self.t["tag"].values()) + 12)
        wrap = w - 2 * self.PADX - self.tag_w

        finals = [self.segments[i] for i in self.order[-self.s["lines"]:]]
        items = [(seg, False) for seg in finals] + [(m, True) for m in self.live.values()]
        items = items[-max(self.s["lines"], len(self.live)):]
        blocks = [(self._kind(it), self._lines_for(it, live, n == len(items) - 1))
                  for n, (it, live) in enumerate(items)]

        need = self._layout(blocks, wrap)
        while need > sh * 0.5 and len(blocks) > 1:  # never cover more than half the screen
            blocks = blocks[1:]
            need = self._layout(blocks, wrap)

        # Grow at once, shrink only after the content has stayed smaller for a while.
        now = time.monotonic()
        if need >= self.height or self.height == 0:
            self.height, self.shrink_since = need, None
        elif self.shrink_since is None:
            self.shrink_since = now
        elif now - self.shrink_since > 4:
            self.height, self.shrink_since = need, None
        if self.height > need:  # keep the newest line at the bottom edge
            self.canvas.move("cap", 0, self.height - need)
        self._place(w, int(self.height))

    def _status_text(self):
        def bar(level):
            db = 20 * np.log10(level + 1e-6)
            n = int(max(0, min(5, (db + 60) / 10)))
            return "▮" * n + "▯" * (5 - n)

        t = self.t
        parts = [t["connected"] if self.connected else t["disconnected"]]
        for kind in ("mic", "system"):
            cap = self.captures.get(kind)
            if cap is None or not self.s[kind]:
                parts.append(f"{t[kind]} {t['off']}")
            elif cap.error:
                parts.append(f"{t[kind]} {t['unavailable']}")
            else:
                n = len(getattr(cap, "devices", ()))
                label = f"{t[kind]}×{n}" if n > 1 else t[kind]
                parts.append(f"{label} {bar(cap.level)}")
        meeting = self.meeting if self.connected else {}
        if meeting.get("recording") and meeting.get("started"):
            sec = int(time.time() - meeting["started"])
            parts.append(f"{t['recording']} {sec // 60}:{sec % 60:02d}")
        for job in (meeting.get("summarizing") or {}).values():
            parts.append(t["summarizing"].format(d=job.get("done", 0), t=job.get("total") or "…"))
        text, until = self.notice
        if text and time.monotonic() < until:
            parts.append(text)
        return "   ".join(parts)

    def _status_color(self):
        return "#3ecf6e" if self.connected else "#e5484d"

    def _tick_status(self):
        if self.layered:
            self._check_layers()
        # Only the status text changes here: update it in place, no relayout.
        if hasattr(self, "status_item"):
            text, color = self._status_text(), self._status_color()
            for item in self.canvas.find_withtag("status"):
                fill = color if item == self.status_item else self.OUTLINE
                self.canvas.itemconfigure(item, text=text, fill=fill)
        self.root.after(250, self._tick_status)

    def run(self):
        self._render()
        self.root.mainloop()


def main():
    p = argparse.ArgumentParser(description="RealTran desktop client: meeting audio in, subtitles out")
    p.add_argument("--ssh", help='SSH target of the RealTran server, e.g. "user@192.168.1.10"')
    p.add_argument("--server-port", type=int, default=8766, help="RealTran's localhost port on the server")
    p.add_argument("--server", help="connect directly instead of via SSH, e.g. wss://192.168.1.10:8765")
    p.add_argument("--mic-device", help="microphone name (substring); default: system default")
    p.add_argument("--system-device", help="capture this input instead of speaker loopback (macOS: BlackHole)")
    p.add_argument("--no-duck", action="store_true",
                   help="keep the mic open while remote participants talk (only if you wear headphones)")
    p.add_argument("--list-devices", action="store_true")
    p.add_argument("--test-audio", action="store_true",
                   help="measure the sound arriving from every speaker (loopback) and microphone, then exit")
    args = p.parse_args()

    if args.test_audio:
        print(f"RealTran client {CLIENT_VERSION} ({platform.platform()})")
        test_audio()
        return

    if args.list_devices:
        import soundcard as sc
        print("Microphones:", *[f"  {m.name}" for m in sc.all_microphones()], sep="\n")
        default = sc.default_speaker().id
        print("Speakers (system audio is captured from all of them):",
              *[f"  {s.name}{'   <- default' if s.id == default else ''}" for s in sc.all_speakers()], sep="\n")
        return

    print(f"RealTran client {CLIENT_VERSION} (Python {platform.python_version()}, {platform.platform()}, Tk {tk.TkVersion})")
    if SYSTEM == "Windows":
        try:  # crisp text on high-DPI screens
            import ctypes
            ctypes.windll.shcore.SetProcessDpiAwareness(1)
        except Exception:
            pass

    tunnel = None
    if args.server:
        url = args.server.replace("https://", "wss://").replace("http://", "ws://")
    elif args.ssh:
        tunnel = SshTunnel(args.ssh, args.server_port)
        tunnel.start()
        print(f"Opening SSH tunnel to {args.ssh} (local port {tunnel.local_port}) ...")
        if tunnel.wait_ready():
            print("SSH tunnel is up.")
        else:
            print("SSH tunnel did not come up; check that `ssh " + args.ssh + "` works without a password.")
        url = f"ws://127.0.0.1:{tunnel.local_port}"
    else:
        url = f"ws://127.0.0.1:{args.server_port}"  # running on the server itself

    state = State(load_settings(), duck=not args.no_duck)
    state.tunnel = tunnel
    net = Net(url, state)
    net.start()
    system_device = args.system_device
    if SYSTEM == "Darwin" and not system_device:
        # macOS cannot record what it plays; a BlackHole virtual device is the usual way round it.
        import soundcard as sc
        blackhole = [m.name for m in sc.all_microphones() if "blackhole" in m.name.lower()]
        system_device = blackhole[0] if blackhole else None
    if system_device:
        system = Capture("system", system_device, state, net.send)
    elif SYSTEM == "Darwin":
        system = Unavailable("macOS cannot capture system audio without BlackHole (see README.md)")
    else:
        system = SystemAudio(state, net.send)
    captures = {"mic": Capture("mic", args.mic_device, state, net.send), "system": system}
    for c in captures.values():
        c.start()
    try:
        Overlay(state, captures, net).run()
    finally:
        if tunnel:
            tunnel.stop()
        sys.exit(0)


if __name__ == "__main__":
    main()
