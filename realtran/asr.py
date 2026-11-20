"""Streaming speech recognition with OpenAI Whisper (transformers backend).

Whisper is not a streaming model, so the live line is produced by re-transcribing the current
utterance every few hundred milliseconds (utterances are short because the VAD cuts them at
pauses). A fast model (large-v3-turbo) is used for these partial updates and, optionally, a more
accurate one (large-v3) for the final transcript of each sentence.

Whisper-specific guards:
  - the language is chosen among the meeting languages only (zh / ja / en), so accented speech
    is not transcribed as Korean, Cantonese, ...
  - segments the model itself rates as "no speech" are dropped
  - well-known hallucinations on noise ("ご視聴ありがとうございました", "Thanks for watching") and
    runaway repetitions are discarded; channel end cards ("请不吝点赞 订阅 转发 打赏支持明镜与点点栏目")
    are cut out even when glued to a real sentence
"""
import re
import threading
import zlib
from difflib import SequenceMatcher
from contextlib import nullcontext

import numpy as np
import torch
from transformers import WhisperForConditionalGeneration, WhisperProcessor

SAMPLE_RATE = 16000
MEETING_LANGS = ("zh", "ja", "en")

# Phrases Whisper is known to invent from silence / noise (it was trained on subtitled video).
HALLUCINATIONS = [
    "ご視聴ありがとうございました", "ご清聴ありがとうございました", "チャンネル登録", "最後までご視聴",
    "字幕", "謝謝觀看", "谢谢观看", "感谢观看", "请不吝点赞", "订阅", "打赏支持",
    "明镜与点点", "优优独播剧场", "thank you for watching", "thanks for watching",
    "please subscribe", "subtitles by",
]
_NORMALIZE = re.compile(r"[\s、。，,.!！?？…・\-\"'「」]+")
# Whole sentences of that kind that are never meeting speech (channel end cards, subtitle credits):
# they are cut out wherever they appear, also when Whisper glues one to a real sentence, which the
# rule above (phrase ≈ whole utterance) does not catch.
_SEP = r"[\s、。，,.!！?？…・]*"
_STRIP = re.compile("|".join([
    # end card of a Chinese YouTube channel: 请不吝点赞 订阅 转发 打赏支持明镜与点点栏目
    rf"(?:请不吝)?点赞{_SEP}订阅{_SEP}转发{_SEP}打赏(?:支持)?(?:{_SEP}明镜与点点(?:栏目)?)?",
    rf"明镜需要您的支持{_SEP}欢迎订阅明镜",
    r"字幕由\s*Amara\.org\s*社区提供",
    r"优优独播剧场(?:\s*[-—]+\s*YoYo Television Series Exclusive)?",
]), re.IGNORECASE)

# Per-language initial prompts: the Chinese one is the well-known way to get Simplified characters
# with punctuation (Whisper otherwise often writes Traditional without punctuation).
LANG_PROMPTS = {
    "zh": "以下是普通话的句子，使用简体中文。",
    "ja": "以下は日本語の会議の発言です。",
    "en": "",
}


def default_device():
    """NVIDIA GPU if present, else the Apple GPU (Metal), else the CPU."""
    if torch.cuda.is_available():
        return "cuda:0"
    if torch.backends.mps.is_available():
        return "mps"
    return "cpu"


def _is_mlx(model_id):
    try:
        from .mlx_backend import is_mlx
    except ImportError:  # no MLX on this machine (Windows / Linux)
        return False
    return is_mlx(model_id)


def _is_hallucination(text: str) -> bool:
    t = _NORMALIZE.sub("", text).lower()
    if not t or "amara.org" in t or t == "you":
        return True
    # Only drop when the phrase is (almost) the whole utterance: real speech can contain them.
    for h in HALLUCINATIONS:
        h = h.replace(" ", "")
        if h in t and len(t) <= len(h) + 4:
            return True
    return False


def _strip_hallucinations(text: str) -> str:
    """Cut out invented sentences (_STRIP); what remains is the real speech, if any."""
    cleaned, n = _STRIP.subn("", text)
    return cleaned.strip(" 、，,…・") if n else text


def _echoes_prompt(text: str, prompt: str) -> bool:
    t, p = _NORMALIZE.sub("", text), _NORMALIZE.sub("", prompt)
    return bool(t) and (t in p or SequenceMatcher(None, t, p).ratio() > 0.8)


def _is_repetitive(text: str) -> bool:
    data = text.encode("utf-8")
    return len(data) > 60 and len(data) / len(zlib.compress(data)) > 2.6


class _Whisper:
    def __init__(self, model_id, device, terms="", langs=MEETING_LANGS):
        self.processor = WhisperProcessor.from_pretrained(model_id)
        self.tokenizer = self.processor.tokenizer
        self.model = WhisperForConditionalGeneration.from_pretrained(model_id, dtype=torch.float16).to(device).eval()
        self.device = device
        self.langs = langs
        tok = self.tokenizer.convert_tokens_to_ids
        self.sot = tok("<|startoftranscript|>")
        self.no_speech = tok("<|nospeech|>")
        self.lang_ids = {l: tok(f"<|{l}|>") for l in langs}
        # Language style hint + the glossary terms (which bias Whisper's spelling of names).
        self.prompts = {}
        for lang in langs:
            text = " ".join(p for p in (LANG_PROMPTS[lang], terms) if p)
            ids = self.processor.get_prompt_ids(text, return_tensors="pt").to(device) if text else None
            self.prompts[lang] = (text, ids)

    def features(self, wav):
        feats = self.processor.feature_extractor(wav, sampling_rate=SAMPLE_RATE, return_tensors="pt").input_features
        return feats.to(self.device, torch.float16)

    @torch.inference_mode()
    def detect(self, feats):
        """Most likely meeting language and the model's no-speech probability."""
        enc = self.model.get_encoder()(feats)
        dec = torch.tensor([[self.sot]], device=self.device)
        logits = self.model(encoder_outputs=enc, decoder_input_ids=dec).logits[0, -1].float()
        lang = max(self.langs, key=lambda l: logits[self.lang_ids[l]].item())
        return lang, logits.softmax(-1)[self.no_speech].item()

    @torch.inference_mode()
    def transcribe(self, feats, lang, max_new_tokens, use_prompt=True):
        prompt_text, prompt_ids = self.prompts[lang] if use_prompt else ("", None)
        kw = {"prompt_ids": prompt_ids} if prompt_ids is not None else {}
        out = self.model.generate(
            input_features=feats, language=lang, task="transcribe", max_new_tokens=max_new_tokens,
            num_beams=1, do_sample=False, **kw,
        )
        seq = out.sequences if hasattr(out, "sequences") else out
        text = self.tokenizer.decode(seq[0], skip_special_tokens=True).strip()
        if prompt_text and text.startswith(prompt_text):
            text = text[len(prompt_text):].strip()
        if prompt_text and _echoes_prompt(text, prompt_text):
            # Whisper sometimes "transcribes" its prompt instead of the audio (seen on 20 s of
            # fast continuous speech): decode the same audio again without the prompt.
            return self.transcribe(feats, lang, max_new_tokens, use_prompt=False)
        return text


class StreamingASR:
    """decode(wav, final) -> (lang_code, text). Thread-safe: channels take turns on the GPU."""

    def __init__(self, model_id="openai/whisper-large-v3", partial_model_id=None, device=None,
                 context="", no_speech_threshold=0.6, langs=MEETING_LANGS):
        terms = context.strip()[:200]
        device = device or default_device()
        self.langs = tuple(langs)  # the languages spoken in the meeting: Whisper picks among these only
        self.lock = threading.Lock()
        self.stream = None
        self.run = lambda fn: fn()
        if _is_mlx(model_id):
            from . import mlx_backend
            load = lambda m: mlx_backend.MLXWhisper(m, terms, self.langs, LANG_PROMPTS)
            self.run = mlx_backend.run  # MLX work happens on its own thread, shared with the translator
        else:
            load = lambda m: _Whisper(m, device, terms, self.langs)
            self.stream = torch.cuda.Stream() if str(device).startswith("cuda") else None
        self.final = load(model_id)
        self.partial = load(partial_model_id) if partial_model_id and partial_model_id != model_id else self.final
        self.no_speech_threshold = no_speech_threshold

    def decode(self, wav: np.ndarray, final: bool = False):
        w = self.final if final else self.partial
        # Fast Japanese / Chinese speech can reach ~8 tokens per second; leave headroom.
        max_new = int(24 + 12 * len(wav) / SAMPLE_RATE)
        def recognize():
            with self.lock, torch.cuda.stream(self.stream) if self.stream is not None else nullcontext():
                feats = w.features(wav)
                lang, no_speech = w.detect(feats)
                if no_speech > self.no_speech_threshold:
                    return "", ""
                return lang, w.transcribe(feats, lang, max_new)

        lang, text = self.run(recognize)
        if not text:
            return "", ""
        text = _strip_hallucinations(text)
        if _is_hallucination(text) or _is_repetitive(text):
            return "", ""
        return lang, text
