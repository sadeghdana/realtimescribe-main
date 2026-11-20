"""Script-based zh / ja / en detection.

The ASR language tag can be wrong for code-switched or filler-led utterances ("Hmm, oh yeah..."
tagged as Chinese), and once streaming locks it in through the prefix it sticks. The writing
system of the transcript itself is a more reliable signal for these three languages.
"""
import re

_KANA = re.compile(r"[぀-ゟ゠-ヿｦ-ﾟ]")
_HAN = re.compile(r"[㐀-䶿一-鿿豈-﫿]")
_LATIN_WORD = re.compile(r"[A-Za-z]+(?:'[A-Za-z]+)?")


def detect(text: str, hint: str = "", allowed=None) -> str:
    """Return "zh", "ja", "en" or "" (nothing usable). `hint` breaks zh/ja ties for kanji-only text.
    `allowed`: the meeting's languages; a result outside them falls back to `hint`."""
    lang = _detect(text, hint)
    return hint if allowed and lang not in allowed and hint else lang


def _detect(text, hint):
    kana = len(_KANA.findall(text))
    han = len(_HAN.findall(text))
    words = len(_LATIN_WORD.findall(text))
    cjk = kana + han
    if cjk == 0 and words == 0:
        return hint
    # An English word carries roughly as much as two CJK characters.
    if words * 2 > cjk:
        return "en"
    if kana >= max(1, 0.1 * cjk):
        return "ja"
    if kana == 0 and hint == "ja":
        return "ja"
    return "zh"
