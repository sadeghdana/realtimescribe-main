"""Web UI + WebSockets, one independent session per connection.

/ws?session=<id>              subtitles: every event goes only to the connections of its session
/ws/audio                     audio in: a browser or the desktop client streams Int16 PCM here
/api/meetings?session=<id>    that session's recorded meetings (newest first)
/api/meetings/<id>/<file>     transcript.md or minutes_<lang>.md of one meeting

Sessions: a desktop client is "pc:<host>" (its microphone, its system audio and its subtitle
window share it); a browser picks a random "web:<id>" and keeps it. Captions, history, translation
context, pause and meeting records never cross from one session to another.
"""
import asyncio
import json
import threading
import time
from collections import OrderedDict
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, HTTPException, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse

from .audio import LEGACY, Channels, StreamResampler
from .minutes import Meetings

WEB_DIR = Path(__file__).resolve().parent.parent / "web"
HISTORY_LIMIT = 300  # sentences kept per session for (re)connecting viewers


class Hub:
    def __init__(self, channels: Channels, meetings_dir: Path, transcript_dir: Path | None = None):
        self.channels = channels
        self.loop: asyncio.AbstractEventLoop | None = None
        self.clients: dict[asyncio.Queue, str] = {}  # viewer queue -> its session
        self.history: dict[str, OrderedDict] = {}    # session -> recent finalized sentences
        self.ready = False                           # models loaded
        self.langs = ["zh", "ja", "en"]               # languages the page shows (--langs / --to)
        self.inputs = None                           # this PC's mic / speaker switch (app.LocalInputs)
        self.speech = None                           # this Mac speaks the translations (app.MacSpeech)
        self.lock = threading.Lock()
        # Text is kept on disk only while a session records a meeting (the record button);
        # --log-all additionally logs every sentence of every session, as earlier versions did.
        self.transcript_path = None
        if transcript_dir is not None:
            transcript_dir.mkdir(parents=True, exist_ok=True)
            self.transcript_path = transcript_dir / time.strftime("%Y%m%d_%H%M%S.txt")
        self.meetings = Meetings(meetings_dir, on_change=self.refresh, on_done=self._minutes_done)
        channels.on_change = self.refresh

    # ---- status ------------------------------------------------------------------------------
    def status(self, session):
        c = self.channels
        sources = c.sources(session)
        if not self.ready:
            state = "loading"
        else:
            state = "paused" if c.is_paused(session) else ("listening" if sources else "idle")
        st = {"type": "status", "session": session, "state": state, "sources": sources,
              "meeting": self.meetings.state(session), "langs": self.langs}
        if session == "local" and self.inputs is not None:
            st["input"] = self.inputs.mode
        if session == "local" and self.speech is not None:
            st["speak"] = self.speech.on
        return st

    def refresh(self, session):
        if session is not None:
            self.publish(self.status(session))
            if session != LEGACY and LEGACY in self.clients.values():
                self.publish(self.status(LEGACY))  # old clients see every session's sources

    def set_ready(self):
        self.ready = True
        for session in set(self.clients.values()):
            self.refresh(session)

    def _minutes_done(self, session, mid, lang, ok):
        self.publish({"type": "minutes", "session": session, "id": mid, "lang": lang, "ok": ok})

    # ---- events --------------------------------------------------------------------------------
    def publish(self, msg: dict):
        """Thread-safe: may be called from any worker thread."""
        session = msg.get("session")
        with self.lock:
            if msg["type"] == "final":
                hist = self.history.setdefault(session, OrderedDict())
                hist[msg["id"]] = {**msg, "tr": {}}
                while len(hist) > HISTORY_LIMIT:
                    hist.popitem(last=False)
            elif msg["type"] == "translation":
                seg = self.history.get(session, {}).get(msg["id"])
                if seg is not None:
                    seg["tr"] = msg["tr"]
                    seg["mt_ms"] = msg["ms"]
                    if self.transcript_path:
                        self._append_transcript(seg)
        if msg["type"] == "final":
            self.meetings.add(msg)
        elif msg["type"] == "translation":
            self.meetings.update(msg)
        if self.loop is not None:
            self.loop.call_soon_threadsafe(self._fanout, msg)

    def _fanout(self, msg):
        session = msg.get("session")
        shared = msg["type"] in ("partial", "final", "translation", "drop")
        for q, s in list(self.clients.items()):
            if s == session or (s == LEGACY and shared):
                q.put_nowait(msg)

    def _append_transcript(self, seg):
        ts = time.strftime("%H:%M:%S", time.localtime(seg["t"]))
        kind = seg.get("ch", "").rpartition(":")[2] if ":" in seg.get("ch", "") else ""
        src = f"{seg.get('src', '')}/{kind}" if kind else seg.get("src", "")
        lines = [f"[{ts}] [{seg.get('session')}] {src} ({seg['lang']}) {seg['text']}"]
        lines += [f"    {k}: {v}" for k, v in seg["tr"].items()]
        with open(self.transcript_path, "a", encoding="utf-8") as f:
            f.write("\n".join(lines) + "\n")

    def snapshot(self, session):
        with self.lock:
            if session == LEGACY:
                segments = sorted((s for h in self.history.values() for s in h.values()), key=lambda s: s["id"])
                segments = segments[-HISTORY_LIMIT:]
            else:
                segments = list(self.history.get(session, {}).values())
        return {"type": "hello", "session": session, "segments": segments, "status": self.status(session)}


def create_app(hub: Hub) -> FastAPI:
    @asynccontextmanager
    async def lifespan(_app):
        hub.loop = asyncio.get_running_loop()
        yield

    app = FastAPI(lifespan=lifespan)

    @app.get("/")
    async def index():
        return FileResponse(WEB_DIR / "index.html", headers={"Cache-Control": "no-cache"})

    @app.get("/api/meetings")
    async def meetings(session: str = ""):
        return {"meetings": hub.meetings.list(session), "state": hub.meetings.state(session)}

    @app.get("/api/meetings/{mid}/{name}")
    async def meeting_file(mid: str, name: str, download: int = 0):
        try:
            path = hub.meetings.path(mid, name)
        except ValueError:
            raise HTTPException(400, "bad request")
        if not path.exists():
            raise HTTPException(404, "not found")
        filename = f"{mid}_{name}" if download else None
        return FileResponse(path, media_type="text/markdown; charset=utf-8", filename=filename,
                            content_disposition_type="attachment" if download else "inline")

    @app.websocket("/ws")
    async def ws(websocket: WebSocket):
        await websocket.accept()
        # No session = a client older than 2026.10.23-11: give it the old shared behaviour.
        session = (websocket.query_params.get("session") or "").strip()[:80] or LEGACY
        q: asyncio.Queue = asyncio.Queue()
        hub.clients[q] = session
        try:
            await websocket.send_text(json.dumps(hub.snapshot(session), ensure_ascii=False))

            async def sender():
                while True:
                    msg = await q.get()
                    await websocket.send_text(json.dumps(msg, ensure_ascii=False))

            async def receiver():
                while True:
                    data = json.loads(await websocket.receive_text())
                    cmd = data.get("cmd")
                    if cmd == "pause":
                        hub.channels.set_paused(session, True)
                    elif cmd == "resume":
                        hub.channels.set_paused(session, False)
                    elif cmd == "input" and session == "local" and hub.inputs is not None and hub.ready:
                        # starting a capture can wait for macOS: keep the event loop free
                        error = await asyncio.to_thread(hub.inputs.set, str(data.get("mode")))
                        if error:
                            await q.put({"type": "input_error", "msg": error})
                        hub.refresh(session)
                    elif cmd == "speak" and session == "local" and hub.speech is not None:
                        hub.speech.set(bool(data.get("on")))
                        hub.refresh(session)
                    elif cmd == "meeting_start":
                        hub.meetings.start(session)
                    elif cmd == "meeting_stop":
                        # file I/O + queueing only; the summary itself runs on a worker thread
                        hub.meetings.stop(session, str(data.get("lang") or "ja"))
                    elif cmd == "summarize":
                        try:
                            hub.meetings.request(str(data.get("id")), str(data.get("lang") or "ja"), session=session)
                        except (ValueError, OSError):
                            pass

            done, pending = await asyncio.wait(
                [asyncio.create_task(sender()), asyncio.create_task(receiver())],
                return_when=asyncio.FIRST_EXCEPTION,
            )
            for t in pending:
                t.cancel()
            for t in done:
                t.exception()  # mark as retrieved; a disconnect is the normal way out
        except WebSocketDisconnect:
            pass
        finally:
            hub.clients.pop(q, None)

    @app.websocket("/ws/audio")
    async def audio_ws(websocket: WebSocket):
        """First message: {"sr": rate, "name": label, then either "client": host + "channel":
        "mic"|"system" (desktop client) or "session": id (browser)}; then binary Int16 mono frames.
        """
        await websocket.accept()
        ch = None
        try:
            hello = json.loads(await websocket.receive_text())
            if not hub.channels.ready:
                await websocket.send_text(json.dumps({"type": "error", "msg": "loading"}))
                await websocket.close()
                return
            resample = StreamResampler(int(hello["sr"]))
            label = str(hello.get("name") or "").strip()[:40] or "browser"
            client = str(hello.get("client") or "").strip()[:64]
            if client:
                session = f"pc:{client}"
                key, name = f"{session}:{hello.get('channel', 'mic')}", label
            else:
                session = str(hello.get("session") or "").strip()[:80] or "room"
                host = websocket.client.host if websocket.client else "?"
                key, name = f"{session}:mic", f"{label} ({host})"
            ch = hub.channels.open(key, name, session)
            await websocket.send_text(json.dumps({"type": "mic", "channel": key}))
            while True:
                data = await websocket.receive_bytes()
                if not hub.channels.is_current(ch):  # the same source connected again elsewhere
                    await websocket.send_text(json.dumps({"type": "revoked"}))
                    await websocket.close()
                    break
                ch.push(resample(data))
        except WebSocketDisconnect:
            pass
        finally:
            if ch is not None:
                hub.channels.close(ch)

    return app
