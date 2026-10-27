"""voice_note: turns the customer's voice note into text.

Everything about the request sits behind transcribe(). Baseten's changelog and Model
APIs overview say Muse Spark Open accepts audio input, but the request format is not
documented. This sends the OpenAI-style input_audio content part with a wav file
(WhatsApp sends ogg/opus, so ffmpeg converts it first). tests/probes/voice_probe.py
checks this live and tries other shapes; change only transcribe() if it says so.

ffmpeg reads only ogg from stdin (no other demuxer, no network or file protocol), stops
after MAX_SECONDS of audio and writes at most MAX_WAV_BYTES. The voice note is heard
once per turn; a second call in the same turn gets the first result.
"""
import base64
import subprocess

from kit import env
from kit.model import EFFORT_REPLY

NAME = "voice_note"
DEFAULT_FFMPEG = "/opt/homebrew/bin/ffmpeg"
MAX_AUDIO_BYTES = 16 * 1024 * 1024
MAX_SECONDS = 120
# 16 kHz mono 16-bit is 32,000 bytes a second, plus the wav header: about 3.84 MB for 120 s.
MAX_WAV_BYTES = 4_000_000
FFMPEG_TIMEOUT = 30
NOT_HEARD = {"ok": False, "feedback": "The voice note could not be heard. Ask the customer to send it again or type it."}

DEFINITION = {"type": "function", "function": {
    "name": NAME,
    "description": "Listen to the voice note the customer sent in this message and get its words as text.",
    "parameters": {"type": "object", "properties": {}, "additionalProperties": False},
}}

PROMPT = (
    "Write down exactly what is said in this voice note, in the language and script the speaker would type: "
    "Egyptian Arabic in Arabic letters, English in English. Only the words, nothing else.")


def check(args, ctx):
    if args:
        return False, "voice_note takes no arguments."
    audio = getattr(ctx.message, "audio", None)
    if not audio:
        return False, "There is no voice note in this message."
    if len(audio) > MAX_AUDIO_BYTES:
        return False, "The voice note is too long to hear. Ask the customer to type it or send a shorter one."
    return True, ""


def run(args, ctx):
    cache = ctx.cache
    if NAME in cache:
        return cache[NAME]
    cache[NAME] = dict(NOT_HEARD)  # held even if transcribe fails, so the turn never hears it twice
    text = transcribe(ctx.message.audio, ctx.message.audio_mime, ctx.model)
    if text:
        cache[NAME] = {"ok": True, "text": text}
    return cache[NAME]


def command(ffmpeg):
    """The ffmpeg argv: ogg from stdin only, at most MAX_SECONDS out, at most MAX_WAV_BYTES written."""
    return [ffmpeg, "-hide_banner", "-loglevel", "error", "-nostdin",
            "-protocol_whitelist", "pipe", "-f", "ogg", "-i", "pipe:0",
            "-t", str(MAX_SECONDS), "-fs", str(MAX_WAV_BYTES),
            "-ac", "1", "-ar", "16000", "-f", "wav", "pipe:1"]


def to_wav(audio, ffmpeg=None, timeout=FFMPEG_TIMEOUT):
    """An ogg voice note to 16 kHz mono wav. List args, no shell, a timeout and a size cap."""
    ffmpeg = ffmpeg or env.get("FFMPEG", DEFAULT_FFMPEG)
    out = subprocess.run(command(ffmpeg), input=audio, capture_output=True, timeout=timeout, check=True, shell=False)
    if len(out.stdout) > MAX_WAV_BYTES:
        raise ValueError("the voice note is longer than the cap")
    return out.stdout


def audio_message(wav):
    return {"role": "user", "content": [
        {"type": "text", "text": PROMPT},
        {"type": "input_audio", "input_audio": {"data": base64.b64encode(wav).decode("ascii"), "format": "wav"}},
    ]}


def transcribe(audio, mime, model):
    """Audio bytes to text, or "" if it could not be heard. The one place the request format lives."""
    wav = audio if (mime or "").startswith("audio/wav") else to_wav(audio)
    reply = model.chat([audio_message(wav)], effort=EFFORT_REPLY)
    return (reply.get("content") or "").strip()
