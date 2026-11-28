"""Live transcript (1.1.0): speech to text while recording, on this PC only.

The speech engine (sherpa-onnx with the SenseVoiceSmall model and Silero voice
detection) comes in a separate download, the Live Transcript pack, which is
unzipped into the program folder. Without it the recorder works as before.

Modules:
  text        clean-up of recognised text (characters, names list, masking); pure
  transcript  transcript entries and the files they are saved to; pure
  retention   files to delete after a number of days; pure
  pack        finds and checks the Live Transcript pack
  live        phrase-by-phrase recognition with provisional text; pure apart from
              the decoder and voice detector it is given
  worker      the separate process that runs the speech engine
  service     the main-process side: feeds audio, collects results, writes files
"""

SR = 16000                      # the speech models' sample rate

LANGUAGES = [                   # setting value, label, SenseVoice language code
    ("auto", "Automatic: English, Cantonese or Mandarin, also mixed", ""),
    ("en", "English", "en"),
    ("yue", "Cantonese", "yue"),
    ("zh", "Mandarin", "zh"),
]
SCRIPTS = [                     # setting value, label
    ("hk", "Traditional (Hong Kong)"),
    ("tw", "Traditional (Taiwan)"),
    ("cn", "Simplified"),
]
SAVE_MODES = [                  # setting value, label
    ("audio_text", "Audio and text"),
    ("text_check", "Text, with audio kept for checking"),
    ("text_only", "Text only: no sound is saved"),
]
SPEAKERS = {"me": "Me", "others": "Others"}


def sense_voice_code(language: str) -> str:
    for value, _label, code in LANGUAGES:
        if value == language:
            return code
    return ""


def language_label(language: str) -> str:
    for value, label, _code in LANGUAGES:
        if value == language:
            return label.split(":")[0]
    return "Automatic"


def script_label(script: str) -> str:
    return dict(SCRIPTS).get(script, "Traditional (Hong Kong)")
