"""Meeting minutes: record a meeting on demand and summarise it with the local LLM.

Nothing is written to disk until someone presses "record", and a recording only keeps the
sentences of the session (desktop client / browser) that started it. From then on every finalized
sentence (with its translations) goes to meetings/<id>/transcript.md; when the recording is
stopped, the translation model running on this PC turns it into minutes in the requested
language (meetings/<id>/minutes_<lang>.md). No text leaves the machine.

Long meetings are summarised in two passes — notes for each ~10-minute chunk, then the minutes
from those notes — so a prompt never grows large enough to exhaust GPU memory, and live
translation can still slip in between the chunks.
"""
import hashlib
import json
import queue
import re
import shutil
import threading
import time
from collections import OrderedDict
from pathlib import Path

from .audio import LEGACY

LANGS = ("zh", "ja", "en")
LANG_NAMES = {"zh": "Simplified Chinese", "ja": "Japanese", "en": "English"}
ID_RE = re.compile(r"^\d{8}_\d{6}(_[0-9a-f]{8})?$")
CHUNK_TOKENS = 2000  # transcript per notes call (~10 minutes of talk)

TEXT = {
    "ja": {"title": "議事録", "date": "日時", "duration": "所要時間", "count": "発言数", "min": "分",
           "sections": ["概要", "議題と主な議論", "決定事項", "ToDo（担当・期限）", "未決事項・次回の確認事項"],
           "none": "特になし", "no_owner": "担当未定",
           "note": "音声認識と AI による自動要約です。重要な内容は文字起こし（transcript）で確認してください。",
           "kind": {"system": "オンライン", "mic": "会場"}},
    "zh": {"title": "会议纪要", "date": "时间", "duration": "时长", "count": "发言数", "min": "分钟",
           "sections": ["概要", "议题与主要讨论", "决定事项", "待办事项（负责人・期限）", "未决事项・下次确认"],
           "none": "无", "no_owner": "负责人未定",
           "note": "本纪要由语音识别和 AI 自动生成，重要内容请以文字记录（transcript）为准。",
           "kind": {"system": "线上", "mic": "现场"}},
    "en": {"title": "Meeting minutes", "date": "Date", "duration": "Duration", "count": "Utterances", "min": "min",
           "sections": ["Summary", "Topics and discussion", "Decisions", "Action items (owner, due)", "Open questions"],
           "none": "None", "no_owner": "owner not stated",
           "note": "Generated automatically from speech recognition by AI; check the transcript for anything important.",
           "kind": {"system": "online", "mic": "room"}},
}

MINUTES_PROMPT = """You write the minutes of an internal business meeting held in Chinese, Japanese and English.
The input is {what}. It comes from live speech recognition and machine translation, so expect recognition errors, fillers and fragments: infer the intended meaning and ignore small talk and noise.
Write the minutes in {language}, in Markdown, with exactly these level-2 headings in this order:
{headings}
Rules:
- State only what the input supports. Never invent names, numbers, dates, owners or decisions. A section with nothing to report contains only "{none}".
- Concise bullet points, one idea each. Keep product names, numbers and technical terms exactly as given.
- Action items: who / what / by when, only as stated; if no owner was stated write "{no_owner}".
- Output only the minutes: no title, no preamble, no closing remarks."""

NOTES_PROMPT = """You take notes on part {i} of {n} of an internal business meeting held in Chinese, Japanese and English. The transcript comes from live speech recognition and machine translation, so expect recognition errors: infer the intended meaning, ignore small talk and noise.
Write concise bullet notes in {language}: topics discussed, key points and numbers, decisions, action items (who / what / when, only if stated) and open questions. Only what the transcript supports; never invent anything. Output only the bullets."""


def _kind(seg):
    ch = seg.get("ch", "")
    return "system" if ch.endswith(":system") else "mic"


class Summarizer:
    """Turns a recorded meeting into minutes with the translation LLM."""

    def __init__(self, translator):
        self.llm = translator

    def summarize(self, meta, segments, lang, progress):
        t = TEXT[lang]
        lines = []
        for s in segments:
            text = s["text"] if s["lang"] == lang else (s.get("tr") or {}).get(lang) or s["text"]
            lines.append(f"[{time.strftime('%H:%M', time.localtime(s['t']))}] {t['kind'][_kind(s)]}: {text}")

        chunks, cur, size = [], [], 0
        for line in lines:
            n = self.llm.count_tokens(line) + 1
            if cur and size + n > CHUNK_TOKENS:
                chunks.append(cur)
                cur, size = [], 0
            cur.append(line)
            size += n
        if cur:
            chunks.append(cur)

        language = LANG_NAMES[lang]
        rules = dict(language=language, none=t["none"], no_owner=t["no_owner"],
                     headings="\n".join(f"## {h}" for h in t["sections"]))
        if len(chunks) == 1:
            progress(0, 1)
            body = self.llm.chat([
                {"role": "system", "content": MINUTES_PROMPT.format(what="a time-stamped transcript", **rules)},
                {"role": "user", "content": "\n".join(chunks[0])},
            ], 1500)
            progress(1, 1)
        else:
            total = len(chunks) + 1
            notes = []
            for i, chunk in enumerate(chunks, 1):
                progress(i - 1, total)
                notes.append(self.llm.chat([
                    {"role": "system", "content": NOTES_PROMPT.format(i=i, n=len(chunks), language=language)},
                    {"role": "user", "content": "\n".join(chunk)},
                ], 600))
            progress(len(chunks), total)
            joined = "\n\n".join(f"### Part {i}\n{n}" for i, n in enumerate(notes, 1))
            body = self.llm.chat([
                {"role": "system", "content": MINUTES_PROMPT.format(
                    what="notes taken from consecutive parts of the meeting", **rules)},
                {"role": "user", "content": joined},
            ], 1500)
            progress(total, total)

        start, end = meta["started"], meta.get("ended") or meta["started"]
        minutes = max(1, round((end - start) / 60))
        header = [
            f"# {t['title']}",
            "",
            f"- {t['date']}: {time.strftime('%Y-%m-%d %H:%M', time.localtime(start))}"
            f" – {time.strftime('%H:%M', time.localtime(end))}",
            f"- {t['duration']}: {minutes} {t['min']}",
            f"- {t['count']}: {len(segments)}",
            "",
            f"> {t['note']}",
            "",
        ]
        return "\n".join(header) + "\n" + body.strip() + "\n"


class Meetings:
    """Recording state, files on disk and the background summary queue — per session.

    Each session (one desktop client, one browser) records on its own: different meetings can be
    recorded at the same time, and a record only ever contains its own session's sentences.
    """

    def __init__(self, root: Path, on_change, on_done):
        self.root = root
        self.root.mkdir(parents=True, exist_ok=True)
        self.on_change = on_change   # callback(session): its recording / summary state changed
        self.on_done = on_done       # callback(session, id, lang, ok): a summary finished
        self.summarizer = None       # set once the translation model is loaded
        self.lock = threading.Lock()
        self.recording = {}          # session -> the meeting it is recording
        self.open = {}               # id -> meeting still in memory (recording or recently stopped)
        self.progress = {}           # id -> {"session", "lang", "done", "total"} while summarising
        self.jobs: "queue.Queue[tuple[str, str]]" = queue.Queue()
        threading.Thread(target=self._worker, daemon=True).start()

    # ---- state -------------------------------------------------------------------------------
    def state(self, session):
        with self.lock:
            m = self.recording.get(session)
            return {
                "recording": bool(m),
                "id": m["meta"]["id"] if m else None,
                "started": m["meta"]["started"] if m else None,
                "count": len(m["segments"]) if m else 0,
                "summarizing": {mid: {k: v for k, v in p.items() if k != "session"}
                                for mid, p in self.progress.items() if p["session"] == session},
                "available": self.summarizer is not None,
            }

    def _dir(self, mid):
        if not ID_RE.match(mid or ""):
            raise ValueError("bad meeting id")
        return self.root / mid

    def _session_of(self, mid):
        meeting = self.open.get(mid)
        if meeting:
            return meeting["meta"].get("session")
        return json.loads((self._dir(mid) / "meta.json").read_text(encoding="utf-8")).get("session")

    # ---- recording ---------------------------------------------------------------------------
    def start(self, session):
        with self.lock:
            if session in self.recording:
                return
            now = time.time()
            # the suffix keeps two sessions that start in the same second apart
            tag = hashlib.sha1(str(session).encode()).hexdigest()[:8]
            mid = f"{time.strftime('%Y%m%d_%H%M%S', time.localtime(now))}_{tag}"
            self._dir(mid).mkdir(parents=True, exist_ok=True)
            meeting = {"meta": {"id": mid, "session": session, "started": now, "ended": None, "count": 0,
                                "minutes": {}},
                       "segments": OrderedDict()}
            if session == LEGACY:  # an old client's record holds everything, like before: no owner
                del meeting["meta"]["session"]
            self.recording[session] = self.open[mid] = meeting
            self._save(meeting)
        self.on_change(session)

    def stop(self, session, lang="ja"):
        with self.lock:
            meeting = self.recording.pop(session, None)
            if not meeting:
                return
            mid = meeting["meta"]["id"]
            if not meeting["segments"]:  # nothing was said: leave no empty record behind
                self.open.pop(mid, None)
                shutil.rmtree(self._dir(mid), ignore_errors=True)
            else:
                meeting["meta"]["ended"] = time.time()
                self._save(meeting)
        self.on_change(session)
        if meeting["segments"]:
            self.request(mid, lang, session=session)

    def add(self, seg):
        """A finalized sentence (called for every one; kept only if its session is recording)."""
        session = seg.get("session")
        with self.lock:
            targets = [(s, self.recording[s]) for s in (session, LEGACY) if s in self.recording]
            for _, meeting in targets:
                meeting["segments"][seg["id"]] = {**seg, "tr": {}}
                self._save(meeting)
        for s, _ in targets:
            self.on_change(s)

    def update(self, msg):
        """Its translation arrived (possibly just after the recording was stopped)."""
        with self.lock:
            for meeting in self.open.values():  # may sit in its own record and a legacy one
                seg = meeting["segments"].get(msg["id"])
                if seg is not None:
                    seg["tr"] = msg["tr"]
                    self._save(meeting)

    def _save(self, meeting):
        """Rewrite transcript.md + segments.json (a meeting is at most a few thousand lines)."""
        meta, segs = meeting["meta"], list(meeting["segments"].values())
        meta["count"] = len(segs)
        d = self._dir(meta["id"])
        start = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(meta["started"]))
        end = time.strftime("%H:%M:%S", time.localtime(meta["ended"])) if meta["ended"] else "…"
        out = [f"# Transcript {start} – {end}", ""]
        for s in segs:
            src = f"{s.get('src', '')}/{_kind(s)}"
            out.append(f"[{time.strftime('%H:%M:%S', time.localtime(s['t']))}] {src} ({s['lang']}) {s['text']}")
            out += [f"    {k}: {v}" for k, v in s["tr"].items()]
        (d / "transcript.md").write_text("\n".join(out) + "\n", encoding="utf-8")
        (d / "segments.json").write_text(json.dumps(segs, ensure_ascii=False), encoding="utf-8")
        (d / "meta.json").write_text(json.dumps(meta, ensure_ascii=False), encoding="utf-8")

    # ---- listing / files ---------------------------------------------------------------------
    def list(self, session):
        items = []
        for d in sorted(self.root.iterdir(), reverse=True):
            meta_path = d / "meta.json"
            if d.is_dir() and ID_RE.match(d.name) and meta_path.exists():
                try:
                    meta = json.loads(meta_path.read_text(encoding="utf-8"))
                except ValueError:
                    continue
                # Records made before sessions existed have none: they stay visible to everyone.
                if meta.get("session", session) == session or session == LEGACY:
                    items.append(meta)
        return items

    def path(self, mid, name):
        if name != "transcript.md" and not re.fullmatch(r"minutes_(zh|ja|en)\.md", name):
            raise ValueError("bad file name")
        return self._dir(mid) / name

    # ---- summaries ---------------------------------------------------------------------------
    def request(self, mid, lang, session=None):
        """Queue minutes for meeting `mid`; when `session` is given it must own the meeting."""
        if lang not in LANGS:
            lang = "ja"
        owner = self._session_of(mid)  # also validates the id
        if session is not None and owner is not None and session != owner:
            raise ValueError("not this session's meeting")
        owner = owner or session  # a record from before sessions: report to whoever asked
        with self.lock:
            self.progress[mid] = {"session": owner, "lang": lang, "done": 0, "total": 0}
        self.on_change(owner)
        self.jobs.put((mid, lang))

    def _worker(self):
        while True:
            mid, lang = self.jobs.get()
            ok, session = False, None
            try:
                with self.lock:
                    session = (self.progress.get(mid) or {}).get("session")
                ok = self._summarize(mid, lang, session)
            except Exception as e:  # keep the queue alive; the error goes into meta.json
                print(f"[minutes] {mid} ({lang}) failed: {e}", flush=True)
                self._set_meta(mid, error=str(e))
            finally:
                with self.lock:
                    self.progress.pop(mid, None)
                self.on_change(session)
                self.on_done(session, mid, lang, ok)

    def _summarize(self, mid, lang, session):
        if self.summarizer is None:
            raise RuntimeError("no translation model loaded (--no-translate)")
        d = self._dir(mid)
        # Give translations of the last sentences a moment to arrive.
        for _ in range(30):
            with self.lock:
                meeting = self.open.get(mid)
                pending = meeting and any(not s["tr"] for s in meeting["segments"].values())
            if not pending:
                break
            time.sleep(0.5)
        meta = json.loads((d / "meta.json").read_text(encoding="utf-8"))
        segments = json.loads((d / "segments.json").read_text(encoding="utf-8"))
        t0 = time.perf_counter()

        def progress(done, total):
            with self.lock:
                self.progress[mid] = {"session": session, "lang": lang, "done": done, "total": total}
            self.on_change(session)

        text = self.summarizer.summarize(meta, segments, lang, progress)
        (d / f"minutes_{lang}.md").write_text(text, encoding="utf-8")
        print(f"[minutes] {mid} ({lang}): {len(segments)} sentences in {time.perf_counter() - t0:.0f}s", flush=True)
        self._set_meta(mid, minutes_lang=lang)
        with self.lock:
            if all(m["meta"]["id"] != mid for m in self.recording.values()):
                self.open.pop(mid, None)  # done with it: free the memory
        return True

    def _set_meta(self, mid, minutes_lang=None, error=None):
        path = self._dir(mid) / "meta.json"
        with self.lock:
            meta = json.loads(path.read_text(encoding="utf-8"))
            if minutes_lang:
                meta.setdefault("minutes", {})[minutes_lang] = time.time()
                meta.pop("error", None)
            if error:
                meta["error"] = error
            meeting = self.open.get(mid)
            if meeting:
                meeting["meta"].update(minutes=meta.get("minutes", {}), **({"error": error} if error else {}))
            path.write_text(json.dumps(meta, ensure_ascii=False), encoding="utf-8")
