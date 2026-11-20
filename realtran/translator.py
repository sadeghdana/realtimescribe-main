"""LLM translation of finalized utterances into the other meeting languages."""
import queue
import threading
import time
from collections import deque
from contextlib import nullcontext

import torch
from transformers import AutoConfig, AutoModelForCausalLM, AutoTokenizer

from . import langid
from .asr import default_device

LANGS = ("zh", "ja", "en")
LANG_NAMES = {"zh": "Simplified Chinese", "ja": "Japanese", "en": "English"}

SYSTEM_PROMPT = """You are a professional interpreter for an internal business meeting where people speak Chinese, Japanese and English.
Translate the speaker's latest utterance into {target}.
- The text comes from live speech recognition: it may lack punctuation, contain fillers (えーと, 那个, um) or misrecognized words. Infer the intended meaning, drop fillers, and fix obvious recognition errors.
- Keep names, numbers and technical terms accurate. Keep it concise and natural, like a subtitle.
- Output ONLY the {target} translation. No explanations, no quotes, no romanization."""


class OffloadedEmbedding(torch.nn.Module):
    """Keeps a large embedding table in CPU RAM; only the looked-up rows are sent to the GPU."""

    def __init__(self, embedding, device):
        super().__init__()
        self._cpu = [embedding]  # a plain list hides it from .to() / .cuda()
        self.device_out = device

    def forward(self, ids):
        return self._cpu[0](ids.to("cpu")).to(self.device_out)


def load_text_model(model_id, device):
    """Load a chat LLM for text generation.

    Gemma 4 checkpoints are multimodal (vision + audio towers) and carry 2.8B parameters of
    per-layer embeddings (PLE) that are only ever indexed by token id. Translation needs neither
    the towers nor the PLE table on the GPU, so only the text decoder goes to VRAM (~8.7 GB
    instead of ~15 GB) and the PLE lookup happens in CPU RAM.
    """
    if AutoConfig.from_pretrained(model_id).model_type != "gemma4":
        return AutoModelForCausalLM.from_pretrained(model_id, dtype=torch.bfloat16, device_map=device)

    from transformers import Gemma4ForCausalLM, Gemma4ForConditionalGeneration

    full = Gemma4ForConditionalGeneration.from_pretrained(model_id, dtype=torch.bfloat16, device_map="cpu")
    with torch.device("meta"):
        model = Gemma4ForCausalLM(full.config.text_config)
    model.model = full.model.language_model
    model.lm_head = full.lm_head  # tied to the token embedding
    model.generation_config = full.generation_config
    del full
    ple = getattr(model.model, "embed_tokens_per_layer", None)
    if ple is not None:
        model.model.embed_tokens_per_layer = OffloadedEmbedding(ple, device)
    return model.to(device)


class Translator:
    def __init__(self, model_id="google/gemma-4-E4B-it", device=None, glossary="",
                 context_size=3, targets=LANGS):
        from .asr import _is_mlx
        self.mlx = None
        if _is_mlx(model_id):
            from .mlx_backend import MLXTextModel
            self.mlx = MLXTextModel(model_id)
            self.tokenizer = self.mlx.tokenizer
        else:
            device = device or default_device()
            self.tokenizer = AutoTokenizer.from_pretrained(model_id)
            self.tokenizer.padding_side = "left"
            self.model = load_text_model(model_id, device).eval()
        self.glossary = glossary.strip()
        self.targets = tuple(targets)  # languages to translate into (the source language is skipped)
        self.history = deque(maxlen=context_size)
        self.stream = torch.cuda.Stream() if not self.mlx and str(device).startswith("cuda") else None
        # Live translation and minutes generation share the model; they take turns per call.
        self.lock = threading.Lock()

    def _messages(self, text, target, history=(), with_context=True):
        system = SYSTEM_PROMPT.format(target=LANG_NAMES[target])
        if self.glossary:
            system += "\n\nGlossary / proper nouns used in this meeting:\n" + self.glossary
        if with_context and history:
            # Context lives in the system prompt: when it sat next to the utterance in the user
            # turn, Gemma sometimes translated the previous sentence again as well.
            system += ("\n\nEarlier in the meeting (context only; these were already translated, never include "
                       "them in your output):\n" + "\n".join(f"- {h}" for h in history))
        user = f"Translate into {LANG_NAMES[target]}:\n{text}"
        return [{"role": "system", "content": system}, {"role": "user", "content": user}]

    def _generate(self, message_lists, max_new):
        prompts = [
            # enable_thinking=False turns off Gemma 4's reasoning mode; other templates ignore it.
            self.tokenizer.apply_chat_template(m, tokenize=False, add_generation_prompt=True, enable_thinking=False)
            for m in message_lists
        ]
        if self.mlx:
            with self.lock:
                return [d.strip() for d in self.mlx.generate(prompts, max_new)]
        ctx = torch.cuda.stream(self.stream) if self.stream is not None else nullcontext()
        with self.lock, ctx:
            inputs = self.tokenizer(prompts, return_tensors="pt", padding=True).to(self.model.device)
            out = self.model.generate(
                **inputs, max_new_tokens=max_new, do_sample=False,
                temperature=None, top_p=None, top_k=None,
                pad_token_id=self.tokenizer.pad_token_id,
            )
        decoded = self.tokenizer.batch_decode(out[:, inputs["input_ids"].shape[1]:], skip_special_tokens=True)
        return [d.strip() for d in decoded]

    @torch.inference_mode()
    def chat(self, messages, max_new):
        """One free-form generation (used for meeting minutes)."""
        return self._generate([messages], max_new)[0]

    def count_tokens(self, text):
        return len(self.tokenizer.encode(text, add_special_tokens=False))

    def translate(self, text, src_lang, remember=True, history=None):
        """`history`: the recent sentences of the same session (each session has its own)."""
        history = self.history if history is None else history
        result = self.translate_many([(text, src_lang, list(history))])[0]
        if remember:
            history.append(text)
        return result

    @torch.inference_mode()
    def translate_many(self, items):
        """[(text, src_lang, context sentences)] -> [{target: translation}], in one batched
        generation: a backlog of sentences costs little more than a single one."""
        jobs = [(i, text, t, ctx) for i, (text, src, ctx) in enumerate(items) for t in self.targets if t != src]
        results = [{} for _ in items]
        if not jobs:
            return results
        max_new = min(384, 32 + 2 * max(len(text) for _, text, _, _ in jobs))
        outs = self._generate([self._messages(text, t, ctx) for _, text, t, ctx in jobs], max_new)
        # Small models occasionally echo the source instead of translating; retry those once
        # without the conversation context, which is what usually distracts them.
        bad = [(job, out) for job, out in zip(jobs, outs) if langid.detect(out, hint=job[2]) != job[2]]
        if bad:
            retry = self._generate([self._messages(text, t, with_context=False) for (_, text, t, _), _ in bad], max_new)
            fixed = {id(job): out for (job, _), out in zip(bad, retry)}
            outs = [fixed.get(id(job), out) for job, out in zip(jobs, outs)]
        for (i, _, t, _), out in zip(jobs, outs):
            results[i][t] = out
        return results


class TranslationWorker(threading.Thread):
    """Translates finalized segments in arrival order and emits `translation` events."""

    def __init__(self, translator: Translator, emit):
        super().__init__(daemon=True)
        self.translator = translator
        self.emit = emit
        self.queue: "queue.Queue[dict]" = queue.Queue()
        self.histories = {}  # session -> its last few sentences (translation context)
        self.max_batch = 4

    def submit(self, seg):
        self.queue.put(seg)

    def run(self):
        while True:
            batch = [self.queue.get()]
            while len(batch) < self.max_batch:  # sentences that piled up meanwhile go in one batch
                try:
                    batch.append(self.queue.get_nowait())
                except queue.Empty:
                    break
            items = []
            for seg in batch:
                history = self.histories.setdefault(seg.get("session"), deque(maxlen=self.translator.history.maxlen))
                items.append((seg["text"], seg["lang"], list(history)))  # context: what came before it
                history.append(seg["text"])
            t0 = time.perf_counter()
            try:
                trs = self.translator.translate_many(items)
            except Exception as e:  # keep the meeting going even if one batch fails
                print(f"[translate] error on #{batch[0]['id']}..#{batch[-1]['id']}: {e}")
                continue
            ms = int((time.perf_counter() - t0) * 1000)
            for seg, tr in zip(batch, trs):
                self.emit({"type": "translation", "id": seg["id"], "tr": tr, "ms": ms, "session": seg.get("session")})
