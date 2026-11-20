"""Apple Silicon backend: Whisper and the translation LLM on Apple's MLX framework.

On a Mac the GPU shares system RAM with everything else. The PyTorch models need ~16 GB, which
on a 24 GB Mac with a browser open means swapping, and then captions fall minutes behind. The
MLX conversions (Gemma quantized to 4 bits) need ~6 GB and run faster on the Apple GPU.

Used automatically for model folders whose config is an MLX one (see `is_mlx`).

All MLX work runs on one thread (`run`): MLX streams belong to the thread that created them, and
the app calls the models from several threads (one per audio channel, translation, minutes). Jobs
run in arrival order, and the LLM queues a few generated tokens at a time, so a caption never waits
long behind a long generation (meeting minutes).
"""
import json
import queue
import threading
from concurrent.futures import Future
from pathlib import Path

import mlx.core as mx
import numpy as np

_jobs: "queue.Queue[tuple]" = queue.Queue()
_thread = None
_start_lock = threading.Lock()


def _work():
    while True:
        fn, fut = _jobs.get()
        try:
            fut.set_result(fn())
        except BaseException as e:  # handed to the caller
            fut.set_exception(e)


def run(fn):
    """Run fn() on the MLX thread and return its result."""
    global _thread
    if threading.current_thread() is _thread:
        return fn()
    with _start_lock:
        if _thread is None:
            _thread = threading.Thread(target=_work, name="mlx", daemon=True)
            _thread.start()
    fut = Future()
    _jobs.put((fn, fut))
    return fut.result()


def is_mlx(model_path):
    """True for a local MLX model folder (mlx-community conversions carry these markers)."""
    p = Path(model_path)
    if not (p / "config.json").exists():
        return False
    if (p / "weights.safetensors").exists() or (p / "weights.npz").exists():
        return True  # mlx-whisper layout
    cfg = json.loads((p / "config.json").read_text(encoding="utf-8"))
    return "quantization" in cfg or "mlx" in p.name.lower()


class MLXWhisper:
    """Same interface as asr._Whisper: features() -> detect() -> transcribe(). Call on the MLX thread."""

    def __init__(self, model_path, terms, langs, lang_prompts):
        from mlx_whisper.load_models import load_model
        from mlx_whisper.tokenizer import get_tokenizer

        self.model = run(lambda: load_model(model_path, dtype=mx.float16))
        self.tokenizer = get_tokenizer(self.model.is_multilingual, num_languages=self.model.num_languages)
        self.langs = langs
        self.lang_ids = {l: self.tokenizer.to_language_token(l) for l in langs}
        self.prompts = {l: " ".join(p for p in (lang_prompts[l], terms) if p) for l in langs}

    def features(self, wav):
        from mlx_whisper.audio import N_FRAMES, N_SAMPLES, log_mel_spectrogram, pad_or_trim

        mel = log_mel_spectrogram(wav, n_mels=self.model.dims.n_mels, padding=N_SAMPLES)
        mel = pad_or_trim(mel, N_FRAMES, axis=-2).astype(mx.float16)
        return self.model.encoder(mel[None])  # encoded once, reused by detect() and transcribe()

    def detect(self, feats):
        """Most likely meeting language and the model's no-speech probability."""
        logits = self.model.logits(mx.array([[self.tokenizer.sot]]), feats)[0, -1].astype(mx.float32)
        probs = np.array(mx.softmax(logits, axis=-1))
        lang = max(self.langs, key=lambda l: probs[self.lang_ids[l]])
        return lang, float(probs[self.tokenizer.no_speech])

    def transcribe(self, feats, lang, max_new_tokens, use_prompt=True):
        from mlx_whisper.decoding import DecodingOptions, decode

        from .asr import _echoes_prompt

        prompt = self.prompts[lang] if use_prompt else ""
        opts = DecodingOptions(language=lang, task="transcribe", sample_len=max_new_tokens,
                               prompt=prompt or None, without_timestamps=True, fp16=True)
        text = decode(self.model, feats, opts)[0].text.strip()
        if prompt and _echoes_prompt(text, prompt):
            # Whisper sometimes "transcribes" its prompt instead of the audio: decode again without it.
            return self.transcribe(feats, lang, max_new_tokens, use_prompt=False)
        return text


class MLXTextModel:
    """Batched greedy generation with mlx-lm; `tokenizer` works like the Hugging Face one."""

    TOKENS_PER_JOB = 8

    def __init__(self, model_path):
        from mlx_lm import load

        self.model, self.tokenizer = run(lambda: load(model_path))
        self._stream = None

    def generate(self, prompts, max_new):
        """A few generated tokens per MLX job, so captions (Whisper jobs) get in between."""
        from mlx_lm.generate import BatchGenerator

        ids = [self.tokenizer.encode(p, add_special_tokens=False) for p in prompts]  # template has <bos>

        def start():
            if self._stream is None:  # created on the MLX thread, which owns it
                self._stream = mx.new_stream(mx.gpu)
            gen = BatchGenerator(self.model, stop_tokens=[[t] for t in self.tokenizer.eos_token_ids],
                                 stream=self._stream)
            return gen, gen.insert(ids, [max_new] * len(ids))

        def steps():  # a few tokens per job: little hand-over overhead, captions wait ~0.1 s at most
            batch = []
            for _ in range(self.TOKENS_PER_JOB):
                responses = gen.next_generated()
                if not responses:
                    return batch, True
                batch.extend(responses)
            return batch, False

        gen, uids = run(start)
        out = {uid: [] for uid in uids}
        try:
            finished = False
            while not finished:
                responses, finished = run(steps)
                for r in responses:
                    if r.finish_reason != "stop":
                        out[r.uid].append(r.token)
        finally:
            run(gen.close)
        return [self.tokenizer.decode(out[uid]) for uid in uids]
