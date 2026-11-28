"""Live transcript text clean-up: Chinese characters, Cantonese spellings, names list, masking (1.1.0)."""
import importlib.util

import pytest

from vrp.transcribe import language_label, script_label, sense_voice_code
from vrp.transcribe import text as T

HAS_OPENCC = importlib.util.find_spec("opencc") is not None


def test_codes_and_labels():
    assert sense_voice_code("auto") == "" and sense_voice_code("yue") == "yue"
    assert sense_voice_code("nonsense") == ""
    assert language_label("zh") == "Mandarin" and language_label("auto").startswith("Automatic")
    assert script_label("hk") == "Traditional (Hong Kong)" and script_label("cn") == "Simplified"


@pytest.mark.parametrize("raw,keep", [("", False), ("   ", False), ("。", False), ("...", False),
                                      ("<|nospeech|>", True), ("ok", True), ("好", True), ("5", True)])
def test_is_speech_text(raw, keep):
    assert T.is_speech_text(raw) is keep


def test_clean_drops_empty_and_punctuation():
    assert T.clean("") == "" and T.clean(" 。 ") == "" and T.clean(None) == ""


@pytest.mark.skipif(not HAS_OPENCC, reason="OpenCC not installed")
def test_script_conversion_and_cantonese_fixes():
    # SenseVoiceSmall gives Cantonese in Simplified characters
    assert T.clean("呢几个字都表达唔到我想讲嘅意思。", "yue", "hk") == "呢幾個字都表達唔到我想講嘅意思。"
    # 系 stays 系 in OpenCC; the phrase list turns the verb into 係 / 喺 (Cantonese only)
    assert T.clean("你系唔系想去？我系度等你", "yue", "hk") == "你係唔係想去？我喺度等你"
    assert T.clean("这个系统", "yue", "hk") == "這個系統"             # 系統 (system) is untouched
    assert T.clean("唔系", "zh", "hk") == "唔系"                       # only for Cantonese
    assert T.clean("唔系", "yue", "cn") == "唔系"                      # and only for Traditional
    assert T.clean("這個問題", "zh", "cn") == "这个问题"
    assert T.clean("Hello world", "en", "hk") == "Hello world"
    # regression (found while writing these tests): in some phrases OpenCC itself turns 系度 into
    # 係度 (我系度 -> 我係度), so the rule for 系度 did not fire; both are now fixed after the
    # conversion, while 度數 (degrees) is left alone
    assert T.clean("佢系度做嘢", "yue", "hk") == "佢喺度做嘢"
    assert "喺" not in T.clean("溫度系度數", "yue", "hk")
    assert T.clean("係邊個", "yue", "hk") == "係邊個"


def test_convert_script_without_opencc_keeps_text(monkeypatch):
    import builtins
    real = builtins.__import__

    def no_opencc(name, *a, **k):
        if name == "opencc":
            raise ImportError("missing")
        return real(name, *a, **k)
    monkeypatch.setattr(builtins, "__import__", no_opencc)
    monkeypatch.setattr(T, "_converters", {})
    assert T.convert_script("这个", "hk") == "这个"


def test_names_list():
    names = [{"heard": "neo time", "correct": "Neo Time"}, {"heard": "李奧", "correct": "理傲"},
             {"heard": "", "correct": "x"}, "not a dict", {"heard": "cat", "correct": "Kat"}]
    assert T.apply_names("call neo time today", names) == "call Neo Time today"
    assert T.apply_names("NEO TIME rocks", names) == "Neo Time rocks"
    assert T.apply_names("concatenate the cat", names) == "concatenate the Kat"     # whole words only
    assert T.apply_names("我哋李奧時代", names) == "我哋理傲時代"                       # Chinese: anywhere
    assert T.apply_names("x", None) == "x"


@pytest.mark.parametrize("raw,masked", [
    ("mail me at amy.chan+work@example.com.hk now", "mail me at [e-mail] now"),
    ("card 4111 1111 1111 1111 ok", "card [card number] ok"),            # passes the Luhn check
    ("ref 4111 1111 1111 1112 ok", "ref 4111 1111 1111 1112 ok"),        # fails it: not a card number
    ("ID A123456(7) please", "ID [ID number] please"),
    ("ID AB 987654 3 please", "ID [ID number] please"),
    ("call 9123 4567 or +852 2123-4567", "call [phone number] or [phone number]"),
    ("the year 2026 and 15 people", "the year 2026 and 15 people"),
    # regression: the middle of a longer number was masked as phone numbers
    ("account 1234 5678 9012 3456 7 ok", "account 1234 5678 9012 3456 7 ok"),
])
def test_mask_numbers(raw, masked):
    assert T.mask_numbers(raw) == masked


def test_clean_runs_names_before_masking():
    names = [{"heard": "amy", "correct": "Amy"}]
    assert T.clean("amy  on 9123 4567", "en", "hk", names, mask=True) == "Amy on [phone number]"
    assert T.clean("amy on 9123 4567", "en", "hk", names, mask=False) == "Amy on 9123 4567"
