"""Audio inputs.

Every source delivers 16 kHz mono float32 chunks into a `Channel`; each channel has its own
segmentation pipeline, so a PC's microphone and its system audio (remote meeting participants)
can be captioned at the same time without their sentences being mixed up.

Every channel belongs to a session: one per desktop client ("pc:<host>"), one per browser
("web:<id>"). Captions, translation context, pause and meeting records stay inside a session.
"""
import queue
import subprocess
import threading
import time
from pathlib import Path

import numpy as np
import sounddevice as sd
import soundfile as sf
import soxr

SAMPLE_RATE = 16000
# Viewers from clients older than 2026.10.23-11 send no session: they keep the old shared
# behaviour (see every session's captions, pause everything, record everything).
LEGACY = "legacy:*"


def list_input_devices():
    hostapis = sd.query_hostapis()
    rows = []
    for idx, dev in enumerate(sd.query_devices()):
        if dev["max_input_channels"] > 0:
            api = hostapis[dev["hostapi"]]["name"]
            rows.append((idx, dev["name"], api, int(dev["default_samplerate"])))
    return rows


class Channel:
    """One audio stream (a microphone, a PC's system audio, ...). Each gets its own Pipeline."""

    def __init__(self, key, name, manager, session):
        self.key = key
        self.name = name
        self.manager = manager
        self.session = session
        self.queue: "queue.Queue[np.ndarray]" = queue.Queue()
        self.closed = threading.Event()

    @property
    def paused(self):
        return self.manager.is_paused(self.session)

    def push(self, x):
        if not self.paused and not self.closed.is_set():
            self.queue.put(x)


class Channels:
    """Open audio channels, keyed by source. Opening a key that is already open replaces the old
    stream (e.g. a second browser pressing the mic button takes over the "room" microphone)."""

    def __init__(self):
        self.paused_sessions = set()
        self.on_open = None    # callback(channel): starts its pipeline; set once models are loaded
        self.on_change = None  # callback(session) after its channels / pause state change
        self._channels = {}
        self._lock = threading.Lock()

    @property
    def ready(self):
        return self.on_open is not None

    def open(self, key, name, session):
        ch = Channel(key, name, self, session)
        with self._lock:
            old = self._channels.get(key)
            self._channels[key] = ch
        if old is not None:
            old.closed.set()
        self.on_open(ch)
        self._changed(session)
        return ch

    def close(self, ch):
        with self._lock:
            if self._channels.get(ch.key) is ch:
                del self._channels[ch.key]
        ch.closed.set()
        self._changed(ch.session)

    def is_current(self, ch):
        return self._channels.get(ch.key) is ch

    def sources(self, session):
        with self._lock:
            return [{"ch": ch.key, "name": ch.name} for ch in self._channels.values()
                    if ch.session == session or session == LEGACY]

    def is_paused(self, session):
        return session in self.paused_sessions or LEGACY in self.paused_sessions

    def set_paused(self, session, paused):
        (self.paused_sessions.add if paused else self.paused_sessions.discard)(session)
        self._changed(session)

    def _changed(self, session):
        if self.on_change:
            self.on_change(session)

class StreamResampler:
    """Int16 PCM at any rate -> 16 kHz float32, for audio arriving over the network."""

    def __init__(self, in_sr):
        self._rs = soxr.ResampleStream(in_sr, SAMPLE_RATE, 1, dtype="float32") if in_sr != SAMPLE_RATE else None

    def __call__(self, pcm16: bytes) -> np.ndarray:
        x = np.frombuffer(pcm16, dtype="<i2").astype(np.float32) / 32768.0
        return self._rs.resample_chunk(x) if self._rs is not None else x


class MicSource:
    """Captures a local microphone at its native rate and resamples to 16 kHz."""

    def __init__(self, device=None, block_ms=32):
        try:
            info = sd.query_devices(device, "input")
        except (sd.PortAudioError, ValueError) as e:
            raise RuntimeError(
                "No microphone found on this PC. Plug one in, pick one with --device "
                "(see --list-devices), or use a browser on the LAN as the microphone."
            ) from e
        self.name = info["name"]
        self.in_sr = int(info["default_samplerate"])
        self.sink = None
        self._resampler = None
        if self.in_sr != SAMPLE_RATE:
            self._resampler = soxr.ResampleStream(self.in_sr, SAMPLE_RATE, 1, dtype="float32")
        self._stream = sd.InputStream(
            device=device,
            samplerate=self.in_sr,
            channels=1,
            dtype="float32",
            blocksize=int(self.in_sr * block_ms / 1000),
            callback=self._callback,
        )

    def _callback(self, indata, frames, time_info, status):
        x = indata[:, 0].copy()
        if self._resampler is not None:
            x = self._resampler.resample_chunk(x)
        if x.size:
            self.sink(x)

    def start(self):
        self._stream.start()

    def stop(self):
        self._stream.stop()


class LoopbackSource:
    """Captures what this PC is playing (e.g. a Teams / Zoom call) via WASAPI loopback."""

    def __init__(self, speaker=None, block_ms=32, in_sr=48000):
        import soundcard as sc

        spk = sc.get_speaker(speaker) if speaker else sc.default_speaker()
        self._mic = sc.get_microphone(id=str(spk.name), include_loopback=True)
        self.name = f"loopback: {spk.name}"
        self.in_sr = in_sr
        self.block = int(in_sr * block_ms / 1000)
        self.sink = None
        self._resampler = soxr.ResampleStream(in_sr, SAMPLE_RATE, 1, dtype="float32")
        self._thread = threading.Thread(target=self._run, daemon=True)

    def _run(self):
        with self._mic.recorder(samplerate=self.in_sr, channels=1, blocksize=self.block) as rec:
            while True:
                x = rec.record(numframes=self.block)[:, 0].astype(np.float32)
                y = self._resampler.resample_chunk(x)
                if y.size:
                    self.sink(y)

    def start(self):
        self._thread.start()


class MacAudioHelper:
    """The Mac audio helper process (mac_audio.swift, built on first use with the Xcode command line
    tools): it captures what the Mac plays and the microphone, and speaks the English translations.
    All three in one process, so the voice never comes back in as speech: the speaker capture leaves
    the helper's own sound out, and the microphone runs through macOS echo cancellation, which takes
    the voice played by the same audio engine as its reference. If the helper ever fails, the app
    keeps running. One helper per app, shared by the sources below."""

    SWIFT = Path(__file__).with_name("mac_audio.swift")
    BINARY = Path(__file__).resolve().parent.parent / "bin" / "mac-audio"
    _instance = None
    _instance_lock = threading.Lock()

    @classmethod
    def get(cls):
        with cls._instance_lock:
            if cls._instance is None:
                cls._instance = cls()
            return cls._instance

    def __init__(self):
        self.proc = None
        self.lock = threading.Lock()
        self.sinks = {"S": None, "M": None}
        self._mic_resampler = None
        self._replies = {}  # "system" / "mic" -> (event, [reply])

    @classmethod
    def build(cls):
        if cls.BINARY.exists() and cls.BINARY.stat().st_mtime >= cls.SWIFT.stat().st_mtime:
            return
        cls.BINARY.parent.mkdir(exist_ok=True)
        r = subprocess.run(["swiftc", "-O", "-swift-version", "5", "-o", str(cls.BINARY), str(cls.SWIFT)],
                           capture_output=True, text=True)
        if r.returncode != 0:
            raise RuntimeError("could not build the Mac audio helper (install the Xcode command line tools: "
                               "xcode-select --install): " + (r.stderr or r.stdout).strip()[-400:])

    def _ensure(self):
        with self.lock:
            if self.proc is not None and self.proc.poll() is None:
                return
            self.build()
            proc = subprocess.Popen([str(self.BINARY)], stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                    stderr=subprocess.PIPE)
            if proc.stderr.readline().decode(errors="replace").strip() != "ready":
                proc.kill()
                raise RuntimeError("the Mac audio helper did not start")
            self.proc = proc
            threading.Thread(target=self._read_audio, args=(proc,), daemon=True).start()
            threading.Thread(target=self._read_log, args=(proc,), daemon=True).start()

    def _send(self, line):
        self._ensure()
        with self.lock:
            self.proc.stdin.write((line.replace("\n", " ").strip() + "\n").encode())
            self.proc.stdin.flush()

    def _read_audio(self, proc):
        while True:
            head = proc.stdout.read(5)
            if len(head) < 5:
                break
            kind, size = chr(head[0]), int.from_bytes(head[1:], "little")
            x = np.frombuffer(proc.stdout.read(size), dtype=np.float32)
            if kind == "M" and self._mic_resampler is not None:
                x = self._mic_resampler.resample_chunk(x)
            sink = self.sinks.get(kind)
            if x.size and sink is not None:
                sink(x.copy())
        print(f"[audio] Mac audio helper ended (exit code {proc.wait()})")

    def _read_log(self, proc):
        for raw in proc.stderr:
            line = raw.decode(errors="replace").strip()
            what, _, rest = line.partition(": ")
            if what in self._replies and (rest.startswith("ready") or rest.startswith("error")):
                if what == "mic" and rest.startswith("ready"):
                    rate = int(rest.split()[-1])
                    self._mic_resampler = (soxr.ResampleStream(rate, SAMPLE_RATE, 1, dtype="float32")
                                           if rate != SAMPLE_RATE else None)
                event, box = self._replies[what]
                box.append(rest)
                event.set()
            elif what != "speech":
                print(f"[audio] Mac audio helper: {line}")

    def switch(self, what, on):
        """Turn "system" / "mic" capture on or off; raises with macOS's reason if it cannot start."""
        if not on:
            if self.proc is not None and self.proc.poll() is None:
                self._send(f"{what} off")
            return
        event, box = threading.Event(), []
        self._replies[what] = (event, box)
        self._send(f"{what} on")
        if not event.wait(15):
            raise RuntimeError("the Mac audio helper did not answer")
        if box[0].startswith("error"):
            raise RuntimeError(box[0].removeprefix("error: "))

    def speak(self, text):
        if text and text.strip():
            self._send("speak " + text)

    def hush(self):
        if self.proc is not None and self.proc.poll() is None:
            self._send("hush")


class MacSystemAudioSource:
    """What this Mac plays (Zoom / Teams / Meet, a video), also with headphones, without the voice
    reading translations (see MacAudioHelper). macOS asks once for "Screen & System Audio Recording"
    permission for the app this runs in (Terminal, ...); only audio is used, never the screen."""

    def __init__(self):
        self.name = "Mac system audio"
        self.sink = None

    def start(self):
        helper = MacAudioHelper.get()
        helper.sinks["S"] = lambda x: self.sink and self.sink(x)
        try:
            helper.switch("system", True)
        except RuntimeError as e:
            raise RuntimeError(
                f"{e}. Allow it in System Settings > Privacy & Security > Screen & System Audio Recording "
                "for the app running RealTran (e.g. Terminal), quit that app (Cmd+Q) and start RealTran again.")

    def stop(self):
        MacAudioHelper.get().switch("system", False)


class MacMicSource:
    """The default microphone through macOS echo cancellation: the voice reading translations is
    subtracted from what the microphone hears (see MacAudioHelper)."""

    def __init__(self):
        self.name = f"{sd.query_devices(kind='input')['name']} (echo-cancelled)"
        self.sink = None

    def start(self):
        helper = MacAudioHelper.get()
        helper.sinks["M"] = lambda x: self.sink and self.sink(x)
        helper.switch("mic", True)

    def stop(self):
        MacAudioHelper.get().switch("mic", False)


class FileSource:
    """Plays an audio file into the pipeline in (roughly) real time; for testing without a mic."""

    def __init__(self, path, speed=1.0, block_ms=32, tail_silence_sec=2.0):
        wav, sr = sf.read(path, dtype="float32", always_2d=True)
        wav = wav.mean(axis=1)
        if sr != SAMPLE_RATE:
            wav = soxr.resample(wav, sr, SAMPLE_RATE)
        self.wav = np.concatenate([wav, np.zeros(int(tail_silence_sec * SAMPLE_RATE), np.float32)])
        self.name = f"file: {path}"
        self.speed = speed
        self.block = int(SAMPLE_RATE * block_ms / 1000)
        self.sink = None
        self._thread = threading.Thread(target=self._run, daemon=True)

    def _run(self):
        t0 = time.perf_counter()
        for pos in range(0, len(self.wav), self.block):
            self.sink(self.wav[pos : pos + self.block])
            wait = t0 + (pos + self.block) / SAMPLE_RATE / self.speed - time.perf_counter()
            if wait > 0:
                time.sleep(wait)

    def start(self):
        self._thread.start()
