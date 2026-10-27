"""voice_note: turns the customer's voice note into text.

Everything about the request sits behind transcribe(). Baseten's changelog and Model
APIs overview say Muse Spark Open accepts audio input, but the request format is not
documented. This sends the OpenAI-style input_audio content part with a wav file
(WhatsApp sends ogg/opus, so ffmpeg converts it first). tests/probes/voice_probe.py
checks this live and tries other shapes; change only transcribe() if it says so.
"""
import base64
import subprocess

from kit import env
from kit.model import EFFORT_REPLY

NAME = "voice_note"
DEFAULT_FFMPEG = "/opt/homebrew/bin/ffmpeg"
MAX_AUDIO_BYTES = 16 * 1024 * 1024

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
    text = transcribe(ctx.message.audio, ctx.message.audio_mime, ctx.model)
    if not text:
        return {"ok": False, "feedback": "The voice note could not be heard. Ask the customer to send it again or type it."}
    return {"ok": True, "text": text}


def to_wav(audio, ffmpeg=None, timeout=30):
    """Any audio the ffmpeg build reads, to 16 kHz mono wav. List args, no shell."""
    ffmpeg = ffmpeg or env.get("FFMPEG", DEFAULT_FFMPEG)
    cmd = [ffmpeg, "-hide_banner", "-loglevel", "error", "-i", "pipe:0",
           "-ac", "1", "-ar", "16000", "-f", "wav", "pipe:1"]
    out = subprocess.run(cmd, input=audio, capture_output=True, timeout=timeout, check=True)
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
