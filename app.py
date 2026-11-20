"""RealTran: live zh/ja/en meeting subtitles.

    python app.py --list-devices
    python app.py                       # default microphone
    python app.py --device 3            # pick a microphone by index
    python app.py --file meeting.wav    # replay a recording in real time (for testing)
    python app.py --lan                 # https on the LAN: other PCs/phones can watch the subtitles
                                        # and act as the microphone from their browser
"""
import argparse
import asyncio
import itertools
import os
import sys
import threading
import time
import webbrowser
from pathlib import Path

ROOT = Path(__file__).resolve().parent


def default_model(name, repo):
    local = ROOT / "models" / name
    return str(local) if local.exists() else repo


def parse_args():
    p = argparse.ArgumentParser(description="Live Chinese/Japanese/English meeting subtitles")
    p.add_argument("--list-devices", action="store_true", help="list microphones and exit")
    p.add_argument("--device", default=None, help="microphone index or name substring")
    p.add_argument("--file", default=None, help="replay an audio file instead of the microphone")
    p.add_argument("--loopback", action="store_true",
                   help="caption what this PC plays (online meeting audio) instead of the microphone")
    p.add_argument("--input", choices=("mic", "speaker", "both"), default="mic",
                   help="what to caption at start: the microphone, what this computer plays (online meeting "
                        "audio, also with headphones), or both; the page can switch it at any time")
    p.add_argument("--no-local-audio", action="store_true",
                   help="do not open a local device; audio only comes from browsers")
    p.add_argument("--lan", action="store_true",
                   help="listen on all interfaces with a self-signed https certificate")
    p.add_argument("--host", default=None, help="bind address (default 127.0.0.1, or 0.0.0.0 with --lan)")
    p.add_argument("--port", type=int, default=8765, help="https port for the LAN (with --lan)")
    p.add_argument("--local-port", type=int, default=8766, help="http port on localhost (and for SSH tunnels)")
    p.add_argument("--asr-model", default=default_model("whisper-large-v3", "openai/whisper-large-v3"),
                   help="Whisper model for the final transcript of each sentence")
    p.add_argument("--asr-partial-model",
                   default=default_model("whisper-large-v3-turbo", "openai/whisper-large-v3-turbo"),
                   help="faster Whisper model for the live line (same as --asr-model to use one model)")
    p.add_argument("--light", action="store_true",
                   help="use the turbo Whisper model for final transcripts too (saves ~3 GB of VRAM)")
    p.add_argument("--mt-model", default=default_model("gemma-4-E4B-it", "google/gemma-4-E4B-it"))
    p.add_argument("--glossary", default=str(ROOT / "glossary.txt"),
                   help="names / terms that help both recognition and translation")
    p.add_argument("--no-translate", action="store_true", help="captions only, no translation model")
    p.add_argument("--langs", default="zh,ja,en",
                   help="languages spoken in the meeting, e.g. ja,en (speech is recognized as one of these)")
    p.add_argument("--to", default=None,
                   help="languages to translate into, e.g. en (default: all of --langs)")
    p.add_argument("--end-silence-ms", type=int, default=600, help="pause length that ends a sentence")
    p.add_argument("--commit-sentences", type=int, default=2,
                   help="translate once this many sentences are finished, without waiting for a pause (0 = off)")
    p.add_argument("--log-all", action="store_true",
                   help="also log every sentence to transcripts/ (by default text is saved only while recording)")
    p.add_argument("--no-browser", action="store_true")
    return p.parse_args()


def read_glossary(path):
    if not path or not os.path.exists(path):
        return ""
    with open(path, encoding="utf-8") as f:
        lines = [ln.strip() for ln in f if ln.strip() and not ln.lstrip().startswith("#")]
    return "\n".join(lines)


def system_audio_source(audio):
    """What this computer plays: ScreenCaptureKit on a Mac, WASAPI loopback on Windows."""
    return audio.MacSystemAudioSource() if sys.platform == "darwin" else audio.LoopbackSource()


class LocalInputs:
    """This PC's own audio, switched between microphone, speaker (system audio) and both from the
    page. Each gets a channel of its own in the "local" session, so their sentences never mix."""

    KEYS = {"mic": "local", "speaker": "local:system"}
    MODES = {"mic": ("mic",), "speaker": ("speaker",), "both": ("mic", "speaker")}

    def __init__(self, args, audio, channels):
        self.args, self.audio, self.channels = args, audio, channels
        self.mode = "off"
        self.active = {}  # kind -> (source, channel)
        self.lock = threading.Lock()

    def _source(self, kind):
        if kind == "speaker":
            return system_audio_source(self.audio)
        device = self.args.device
        return self.audio.MicSource(int(device) if device is not None and device.isdigit() else device)

    def _start(self, kind):
        """(source, channel), started. On a Mac the default microphone goes through echo cancellation,
        so the voice reading translations aloud is not heard again; the plain one if that fails."""
        candidates = []
        if kind == "mic" and sys.platform == "darwin" and self.args.device is None:
            candidates.append(self.audio.MacMicSource)
        candidates.append(lambda: self._source(kind))
        for i, make in enumerate(candidates):
            source = make()
            ch = self.channels.open(self.KEYS[kind], source.name, "local")
            source.sink = ch.push
            try:
                source.start()
                return source, ch
            except Exception as e:
                self.channels.close(ch)
                if i == len(candidates) - 1:
                    raise
                print(f"[audio] {source.name}: {e}; using the microphone without echo cancellation")

    def set(self, mode):
        """Switch to `mode`; returns an error message, or "" when everything started."""
        if mode not in self.MODES:
            return f"unknown input {mode!r}"
        errors = []
        with self.lock:
            for kind in ("mic", "speaker"):
                if kind not in self.MODES[mode] and kind in self.active:
                    source, ch = self.active.pop(kind)
                    source.stop()
                    self.channels.close(ch)
            for kind in self.MODES[mode]:
                if kind in self.active:
                    continue
                try:
                    self.active[kind] = self._start(kind)
                except Exception as e:
                    errors.append(f"{kind}: {e}")
            on = [k for k in ("mic", "speaker") if k in self.active]
            self.mode = "both" if len(on) == 2 else (on[0] if on else "off")
        for e in errors:
            print(f"[audio] {e}")
        return " / ".join(errors)


class MacSpeech:
    """🔊 on this Mac: the English translations are spoken by the Mac audio helper, which keeps its own
    voice out of the speaker capture and cancels it from the microphone, so it is never transcribed."""

    def __init__(self, audio):
        self.helper = audio.MacAudioHelper.get()
        self.on = False

    def set(self, on):
        self.on = on
        if not on:
            self.helper.hush()

    def say(self, text):
        if self.on and text:
            try:
                self.helper.speak(text)
            except RuntimeError as e:
                print(f"[speech] {e}")


def open_fixed_source(args, audio):
    """--file / --loopback: one fixed source instead of the switchable mic / speaker."""
    if args.file:
        return audio.FileSource(args.file)
    if args.loopback:
        return system_audio_source(audio)
    return None


def main():
    args = parse_args()
    args.langs = tuple(l.strip() for l in args.langs.split(",") if l.strip())
    args.to = tuple(l.strip() for l in args.to.split(",") if l.strip()) if args.to else args.langs
    if all(Path(m).exists() for m in (args.asr_model, args.asr_partial_model, args.mt_model)):
        # Every model is a local folder: forbid any Hugging Face network access at run time.
        os.environ["HF_HUB_OFFLINE"] = "1"

    from realtran import audio
    if args.list_devices:
        for idx, name, api, sr in audio.list_input_devices():
            print(f"[{idx:2d}] {name}  ({api}, {sr} Hz)")
        return

    import uvicorn
    from realtran.server import Hub, create_app

    channels = audio.Channels()
    hub = Hub(channels, ROOT / "meetings", ROOT / "transcripts" if args.log_all else None)
    hub.langs = sorted(set(args.langs) | set(args.to), key="zh ja en".split().index)  # what the page shows
    fixed = None if args.no_local_audio else open_fixed_source(args, audio)
    if not args.no_local_audio and fixed is None:
        hub.inputs = LocalInputs(args, audio, channels)
    if not args.no_local_audio and sys.platform == "darwin":
        hub.speech = MacSpeech(audio)

    def load_and_start():
        import numpy as np
        import transformers
        transformers.logging.set_verbosity_error()
        from realtran.asr import StreamingASR
        from realtran.pipeline import EchoGuard, Pipeline, SegmenterConfig

        glossary = read_glossary(args.glossary)
        asr_context = glossary.replace("\n", ", ")  # Whisper prompt: just the terms themselves

        t0 = time.perf_counter()
        final_model = args.asr_partial_model if args.light else args.asr_model
        print(f"Loading ASR models {Path(final_model).name} + {Path(args.asr_partial_model).name} ...")
        asr = StreamingASR(final_model, args.asr_partial_model, context=asr_context, langs=args.langs)
        asr.decode(np.zeros(16000, np.float32))  # warm-up
        print(f"  ready in {time.perf_counter() - t0:.1f}s")

        # The page can read translations aloud: when they come back in through the system audio
        # (or the microphone), they are recognized as that echo and dropped.
        echo = EchoGuard()

        langs = {}  # sentence id -> spoken language, to know what a translation was translated from

        def publish(msg):
            if msg["type"] == "final":
                langs[msg["id"]] = msg["lang"]
                if len(langs) > 1000:
                    del langs[next(iter(langs))]
            elif msg["type"] == "translation":
                for text in msg["tr"].values():
                    echo.add(text)
                if hub.speech is not None and msg.get("session") == "local" and langs.get(msg["id"]) != "en":
                    hub.speech.say(msg["tr"].get("en"))
            hub.publish(msg)

        on_final = None
        if not args.no_translate:
            from realtran.translator import TranslationWorker, Translator
            t0 = time.perf_counter()
            print(f"Loading translation model {args.mt_model} ...")
            translator = Translator(args.mt_model, glossary=glossary, targets=args.to)
            translator.translate("こんにちは。", "ja", remember=False)  # warm-up
            print(f"  ready in {time.perf_counter() - t0:.1f}s")
            worker = TranslationWorker(translator, publish)
            worker.start()
            on_final = worker.submit
            from realtran.minutes import Summarizer
            hub.meetings.summarizer = Summarizer(translator)

        import torch
        if torch.cuda.is_available():
            torch.cuda.empty_cache()
            free = torch.cuda.mem_get_info()[0] / 2**30
            print(f"VRAM: {torch.cuda.memory_allocated() / 2**30:.1f} GiB used by the models, {free:.1f} GiB still free")
            if free < 1.0 and not args.light:
                # Windows silently spills into system RAM when VRAM runs out, which makes everything slow.
                print("  WARNING: little VRAM left. If subtitles lag, close other GPU programs or restart with --light")
        elif torch.backends.mps.is_available():
            used = torch.mps.current_allocated_memory()
            try:
                import mlx.core as mx
                used += mx.get_active_memory()
            except ImportError:
                pass
            print(f"Apple GPU: {used / 2**30:.1f} GiB used by the models")
        cfg = SegmenterConfig(end_silence_ms=args.end_silence_ms, commit_sentences=args.commit_sentences)
        ids = itertools.count(1)
        channels.on_open = lambda ch: Pipeline(ch, asr, publish, ids, on_final=on_final, cfg=cfg,
                                               echo=echo).start()
        if fixed is not None:
            fixed.sink = channels.open("local", fixed.name, "local").push
            fixed.start()
        if hub.inputs is not None:
            hub.inputs.set(args.input)
        hub.set_ready()
        local = ", ".join(s["name"] for s in channels.sources("local"))
        print(f"Local audio: {local or '(none: audio comes from browsers / desktop clients)'}")
        print("Ready.")

    threading.Thread(target=load_and_start, daemon=True).start()

    app = create_app(hub)
    configs = []
    # This PC's own audio (microphone / --loopback / --file) has a session of its own, "local":
    # the addresses printed below open that session so its captions are what the page shows.
    query = "" if args.no_local_audio else "?session=local"
    if args.lan:
        # LAN browsers need https for the microphone; this PC gets a plain-http port on
        # localhost (a secure context too) so it never sees the certificate warning.
        from realtran.certs import ensure_cert, local_ipv4s
        cert, key = ensure_cert(ROOT / "certs")
        configs.append(uvicorn.Config(app, host=args.host or "0.0.0.0", port=args.port,
                                      ssl_certfile=cert, ssl_keyfile=key, log_level="warning"))
        print("\nOn other PCs / phones in the LAN open:")
        for ip in local_ipv4s():
            print(f"    https://{ip}:{args.port}/{query}")
        print("  (self-signed certificate: accept the browser warning once per device)")
    # Plain http on localhost; the desktop client reaches it through an SSH tunnel.
    local_port = args.local_port
    configs.append(uvicorn.Config(app, host="127.0.0.1", port=local_port, log_level="warning"))
    url = f"http://localhost:{local_port}/{query}"
    print(f"On this PC: {url}\n")
    if not args.no_browser:
        threading.Timer(1.0, lambda: webbrowser.open(url)).start()

    async def serve():
        await asyncio.gather(*(uvicorn.Server(c).serve() for c in configs))

    try:
        asyncio.run(serve())
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()