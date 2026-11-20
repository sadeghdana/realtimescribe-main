"""VAD segmentation + streaming ASR loop, one per audio channel.

Audio is cut into utterances with Silero VAD. While someone is speaking, the current utterance is
re-transcribed every `partial_step_sec` and emitted as a `partial` event (the gray live line).
When the speaker pauses, the utterance is finalized, emitted as `final`, and handed to the
translator. Every channel (a PC's microphone, its system audio, ...) gets its own Pipeline so
that sentences from different sources are never spliced together.

People who talk without pausing would otherwise get no translation until they stop. So while
someone talks, the live transcript is watched: once it holds enough finished sentences (or the
utterance runs long), the audio is cut at the breath where the unfinished sentence starts; the
part before it is finalized and translated right away, the rest carries on as the next sentence.
"""
import queue
import re
import threading
import time
from collections import deque
from dataclasses import dataclass
from difflib import SequenceMatcher

import numpy as np
import torch
from silero_vad import load_silero_vad

from . import langid
from .asr import StreamingASR
from .audio import Channel

SR = 16000
FRAME = 512  # Silero VAD works on 32 ms frames at 16 kHz
FRAME_MS = FRAME * 1000 // SR


@dataclass
class SegmenterConfig:
    vad_threshold: float = 0.5        # speech starts above this probability
    vad_neg_threshold: float = 0.35   # ...and counts as silence below this one
    preroll_ms: int = 300             # audio kept from before speech onset
    end_silence_ms: int = 600         # pause that ends a sentence
    long_end_silence_ms: int = 300    # shorter pause accepted once a sentence gets long
    long_utt_sec: float = 8.0
    max_utt_sec: float = 20.0         # hard cut for people who never pause
    min_speech_ms: int = 250          # ignore clicks / coughs shorter than this
    partial_step_sec: float = 0.5     # how often the live line is refreshed
    # Translate without waiting for a pause (0 disables): as soon as this many sentences are
    # finished, or once the utterance is commit_after_sec long with at least one finished, or
    # (unpunctuated talk) once it is commit_force_sec long at all.
    commit_sentences: int = 2
    commit_after_sec: float = 8.0
    commit_force_sec: float = 12.0


class EchoGuard:
    """Translations the page reads aloud (🔊) come back in through the system audio capture (and the
    microphone without headphones). A recognized sentence that repeats a translation from the last
    minute is that echo, not something someone said."""

    def __init__(self, window_sec=60.0):
        self.window = window_sec
        self.recent = deque()  # (time, normalized translation)
        self.lock = threading.Lock()

    @staticmethod
    def _norm(text):
        return re.sub(r"[^\w]+", "", text.lower())

    def add(self, text):
        n = self._norm(text)
        if n:
            with self.lock:
                self.recent.append((time.monotonic(), n))

    def matches(self, text, partial=False):
        n = self._norm(text)
        if len(n) < 6:
            return False
        with self.lock:
            while self.recent and time.monotonic() - self.recent[0][0] > self.window:
                self.recent.popleft()
            recent = [r for _, r in self.recent]
        for r in recent:
            if n in r or (not partial and SequenceMatcher(None, n, r).ratio() >= 0.6):
                return True
        return False


# A sentence end: 。！？!? or a period followed by a space (so "20.5" does not count).
_SENTENCE_END = re.compile(r"[。！？!?]|\.(?=\s)")


def finished_sentences(text):
    """(finished sentences, characters of the unfinished tail). The sentence at the very end is
    never counted as finished: Whisper tends to close audio that stops mid-sentence with a period."""
    text = text.strip()
    ends = [m.end() for m in _SENTENCE_END.finditer(text) if text[m.end():].strip()]
    if not ends:
        return 0, len(text)
    return len(ends), len(text) - ends[-1]


class Pipeline(threading.Thread):
    def __init__(self, channel: Channel, asr: StreamingASR, emit, ids, on_final=None,
                 cfg: SegmenterConfig = None, echo: EchoGuard = None):
        super().__init__(daemon=True)
        self.channel = channel
        self.asr = asr
        self.emit = emit
        self.ids = ids  # shared counter: segment ids are unique across channels
        self.on_final = on_final
        self.cfg = cfg or SegmenterConfig()
        self.echo = echo
        self.vad = load_silero_vad()

        self.preroll = deque(maxlen=max(1, self.cfg.preroll_ms // FRAME_MS))  # (frame, vad prob)
        self.in_speech = False
        self.utt = []
        self.probs = []  # VAD probability of every frame in self.utt: where the breaths are
        self.commit_retry_at = 0.0
        self.silence_ms = 0
        self.speech_ms = 0
        self.seg_id = 0
        self.lang = ""
        self.last_partial_len = 0
        self.partial_emitted = False

    # ---- main loop -------------------------------------------------------------------------
    def run(self):
        ch = self.channel
        pending = np.zeros(0, np.float32)
        while True:
            try:
                chunks = [ch.queue.get(timeout=0.1)]
            except queue.Empty:
                if ch.closed.is_set():
                    break
                if self.in_speech and ch.paused:
                    self._finalize()
                continue
            while True:  # drain the backlog that piled up while the GPU was busy
                try:
                    chunks.append(ch.queue.get_nowait())
                except queue.Empty:
                    break
            pending = np.concatenate([pending, *chunks])
            n = len(pending) // FRAME
            for i in range(n):
                self._on_frame(pending[i * FRAME:(i + 1) * FRAME])
            pending = pending[n * FRAME:]

            if self.in_speech and self._utt_samples() - self.last_partial_len >= self.cfg.partial_step_sec * SR:
                self._partial()
        if self.in_speech:  # the source went away mid-sentence
            self._finalize()

    # ---- segmentation ----------------------------------------------------------------------
    def _utt_samples(self):
        return len(self.utt) * FRAME

    def _on_frame(self, frame):
        prob = self.vad(torch.from_numpy(frame), SR).item()
        if not self.in_speech:
            self.preroll.append((frame, prob))
            if prob >= self.cfg.vad_threshold:
                self._start_utterance()
            return

        self.utt.append(frame)
        self.probs.append(prob)
        if prob >= self.cfg.vad_neg_threshold:
            self.silence_ms = 0
            self.speech_ms += FRAME_MS
        else:
            self.silence_ms += FRAME_MS

        dur = self._utt_samples() / SR
        need = self.cfg.end_silence_ms if dur < self.cfg.long_utt_sec else self.cfg.long_end_silence_ms
        if self.silence_ms >= need or dur >= self.cfg.max_utt_sec:
            self._finalize()

    def _start_utterance(self):
        self.in_speech = True
        self.utt = [f for f, _ in self.preroll]
        self.probs = [p for _, p in self.preroll]
        self.preroll.clear()
        self.silence_ms = 0
        self.speech_ms = FRAME_MS
        self.seg_id = next(self.ids)
        self.lang = ""
        self.last_partial_len = 0
        self.partial_emitted = False

    def _event(self, type_, **kw):
        return {"type": type_, "id": self.seg_id, "ch": self.channel.key, "src": self.channel.name,
                "session": self.channel.session, **kw}

    # ---- ASR -------------------------------------------------------------------------------
    def _partial(self):
        wav = np.concatenate(self.utt)
        language, text = self.asr.decode(wav)
        self.last_partial_len = len(wav)
        self.lang = langid.detect(text, language or self.lang, self.asr.langs)
        if text and self.echo and self.echo.matches(text, partial=True):
            return  # the page reading a translation aloud
        if text:
            self.partial_emitted = True
            self.emit(self._event("partial", lang=self.lang, text=text))
            if self.cfg.commit_sentences and time.monotonic() >= self.commit_retry_at:
                cut = self._early_cut(text, len(wav) / SR)
                if cut is not None:
                    self._commit(cut)

    # ---- translate without waiting for a pause -----------------------------------------------
    def _early_cut(self, text, dur):
        """Frame index to cut the current utterance at, or None to keep listening."""
        cfg = self.cfg
        done, tail = finished_sentences(text)
        if dur >= cfg.commit_force_sec and done == 0:
            target, lo, hi = dur - 1.0, dur - 6.0, dur - 1.0   # unpunctuated: the latest breath
        elif done >= cfg.commit_sentences or (done >= 1 and dur >= cfg.commit_after_sec) or dur >= cfg.commit_force_sec:
            # The unfinished sentence starts about this far in (assuming a steady speaking rate).
            target = dur * (1 - tail / max(1, len(text.strip())))
            lo, hi = target - 1.2, target + 1.2
        else:
            return None
        cut = self._breath_near(target, lo, hi)
        if cut is None:
            self.commit_retry_at = time.monotonic() + 1.0  # no breath there yet: look again shortly
        return cut

    def _breath_near(self, target, lo, hi):
        """Middle frame of the dip in voice probability (a breath, >= 64 ms) closest to target."""
        lo_f = max(int(1.0 * SR / FRAME), int(lo * SR / FRAME))          # keep >= 1 s before the cut
        hi_f = min(len(self.probs) - int(0.5 * SR / FRAME), int(hi * SR / FRAME))  # and >= 0.5 s after
        best, i = None, lo_f
        while i < hi_f:
            if self.probs[i] < self.cfg.vad_threshold:
                j = i
                while j < hi_f and self.probs[j] < self.cfg.vad_threshold:
                    j += 1
                if j - i >= 2:
                    mid = (i + j) // 2
                    score = abs(mid * FRAME / SR - target) - 0.05 * (j - i)  # nearer and longer is better
                    if best is None or score < best[0]:
                        best = (score, mid)
                i = j
            i += 1
        return best[1] if best else None

    def _commit(self, cut):
        """Finalize everything before frame `cut`; the rest becomes the next sentence."""
        wav = np.concatenate(self.utt[:cut])
        t0 = time.perf_counter()
        language, text = self.asr.decode(wav, final=True)
        asr_ms = int((time.perf_counter() - t0) * 1000)
        if self.echo and self.echo.matches(text):
            text = ""  # the page reading a translation aloud
        if text.strip():
            seg = self._event("final", lang=langid.detect(text, language or self.lang, self.asr.langs), text=text.strip(),
                              t=time.time(), dur=round(len(wav) / SR, 2), asr_ms=asr_ms, early=True)
            self.emit(seg)
            if self.on_final:
                self.on_final(seg)
        elif self.partial_emitted:
            self.emit(self._event("drop"))
        self.utt, self.probs = self.utt[cut:], self.probs[cut:]
        self.seg_id = next(self.ids)
        self.speech_ms = FRAME_MS * sum(p >= self.cfg.vad_neg_threshold for p in self.probs)
        self.last_partial_len = 0
        self.partial_emitted = False

    def _finalize(self):
        self.in_speech = False
        # Keep ~200 ms of the trailing pause; the rest is wasted compute.
        trim = max(0, self.silence_ms // FRAME_MS - 200 // FRAME_MS)
        frames = self.utt[:len(self.utt) - trim] if trim else self.utt
        self.utt, self.probs = [], []
        text = ""
        if self.speech_ms >= self.cfg.min_speech_ms and frames:
            wav = np.concatenate(frames)
            t0 = time.perf_counter()
            language, text = self.asr.decode(wav, final=True)
            asr_ms = int((time.perf_counter() - t0) * 1000)
            if self.echo and self.echo.matches(text):
                text = ""  # the page reading a translation aloud
        if not text.strip():
            if self.partial_emitted:
                self.emit(self._event("drop"))
            return

        seg = self._event("final", lang=langid.detect(text, language or self.lang, self.asr.langs), text=text.strip(),
                          t=time.time(), dur=round(len(wav) / SR, 2), asr_ms=asr_ms)
        self.emit(seg)
        if self.on_final:
            self.on_final(seg)
