"""Clean-up of recognised text: Chinese characters, Cantonese spellings, names list and masking.

Everything here is pure (no files, no UI) and runs in the main process on each
finished phrase, in this order: drop empty text, convert Chinese characters,
fix common Cantonese spellings, apply the names list, mask numbers.

The Chinese conversion and the names-list matching follow the ones in Secure
Offline Transcriber (sot/postproc.py), Neo Time Consulting's sibling app.
"""
from __future__ import annotations

import re

CJK = re.compile(r"[㐀-鿿豈-﫿]")
_WORDISH = re.compile(r"[^\W_]", re.UNICODE)
_OPENCC = {"hk": "s2hk", "tw": "s2tw", "cn": "t2s"}
_converters: dict = {}

# SenseVoiceSmall writes Cantonese in Simplified characters, and OpenCC keeps 系
# (system, also the simplified form of 係). In Cantonese speech these phrases mean
# 係 (to be) or 喺 (at). Only whole phrases are changed, never the single character.
YUE_FIXES = [
    ("系唔系", "係唔係"), ("唔系", "唔係"), ("系咪", "係咪"), ("就系", "就係"), ("都系", "都係"),
    ("仲系", "仲係"), ("咪系", "咪係"), ("系咁", "係咁"), ("系呀", "係呀"), ("系啊", "係啊"),
    ("系喇", "係喇"), ("系囉", "係囉"), ("系點", "係點"), ("系咩", "係咩"), ("系嘅", "係嘅"),
]
# 喺度 (here, at it): OpenCC may already have turned 系度 into 係度, so both are fixed;
# 係度數 / 係度量 (degree, measure) are left alone. 係邊 is left alone: 係邊個 (who is it) is right.
_HAI_DOU = re.compile(r"[系係]度(?![數数量假])")


def has_cjk(text: str) -> bool:
    return bool(CJK.search(text))


def is_speech_text(text: str) -> bool:
    """False for text with no letters, digits or Chinese characters (only punctuation or tags)."""
    return bool(_WORDISH.search(text or ""))


def convert_script(text: str, script: str) -> str:
    """Chinese characters to Traditional (hk / tw) or Simplified (cn). Text without them is unchanged."""
    conf = _OPENCC.get(script)
    if not conf or not has_cjk(text):
        return text
    try:
        if conf not in _converters:
            import opencc
            _converters[conf] = opencc.OpenCC(conf)
        return _converters[conf].convert(text)
    except Exception:            # OpenCC missing or broken: keep the text as recognised
        return text


def fix_cantonese(text: str, script: str) -> str:
    if script not in ("hk", "tw"):
        return text
    for wrong, right in YUE_FIXES:
        text = text.replace(wrong, right)
    return _HAI_DOU.sub("喺度", text)


def apply_names(text: str, names: list, script: str | None = None) -> str:
    """Replace each 'heard' with 'correct' (whole words for Latin text, any position for Chinese)."""
    for r in names or []:
        if not isinstance(r, dict):
            continue
        heard = str(r.get("heard") or "").strip()
        correct = str(r.get("correct") or "")
        if not heard:
            continue
        if script:
            heard, correct = convert_script(heard, script), convert_script(correct, script)
        pat = re.escape(heard)
        if not has_cjk(heard):
            pat = r"(?<!\w)" + pat + r"(?!\w)"
        text = re.sub(pat, lambda _m: correct, text, flags=re.IGNORECASE)
    return text


# ---------------------------------------------------------------- masking
_EMAIL = re.compile(r"[\w.+-]+@[\w-]+(?:\.[\w-]+)+")
_CARD = re.compile(r"(?<!\d)(?:\d[ -]?){12,18}\d(?!\d)")
_HKID = re.compile(r"(?<![A-Za-z0-9])[A-Za-z]{1,2}\s?\d{6}\s?(?:\(\s?[0-9Aa]\s?\)|[0-9Aa])(?![A-Za-z0-9])")
# a phone number stands alone: not the middle of a longer group of numbers (an account or reference number)
_PHONE = re.compile(r"(?<![\w+])(?<!\d[ .-])(?:\+\d{1,3}[ .-]?)?(?:\(\d{2,4}\)[ .-]?)?\d{3,4}[ .-]?\d{4}(?![ .-]?\d)")


def _luhn(digits: str) -> bool:
    total = 0
    for i, ch in enumerate(reversed(digits)):
        d = int(ch)
        if i % 2:
            d *= 2
            if d > 9:
                d -= 9
        total += d
    return total % 10 == 0


def mask_numbers(text: str) -> str:
    """Mask e-mail addresses, card numbers (Luhn-checked), Hong Kong ID numbers and phone numbers."""
    text = _EMAIL.sub("[e-mail]", text)

    def card(m):
        digits = re.sub(r"\D", "", m.group(0))
        return "[card number]" if 13 <= len(digits) <= 19 and _luhn(digits) else m.group(0)
    text = _CARD.sub(card, text)
    text = _HKID.sub("[ID number]", text)
    text = _PHONE.sub("[phone number]", text)
    return text


def clean(text: str, lang: str = "", script: str = "hk", names: list | None = None, mask: bool = False) -> str:
    """One finished phrase as it is shown and saved; "" when there is nothing to keep."""
    text = (text or "").strip()
    if not is_speech_text(text):
        return ""
    text = convert_script(text, script)
    if lang == "yue":
        text = fix_cantonese(text, script)
    text = apply_names(text, names or [], script)
    if mask:
        text = mask_numbers(text)
    return re.sub(r"\s{2,}", " ", text).strip()
