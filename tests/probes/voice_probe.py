"""Live probe: which audio request shape Muse Spark Open on Baseten accepts.

Baseten says the model accepts audio input but does not document the request format.
Run this yourself, with BASETEN_API_KEY in .env, on a real WhatsApp voice note:

    python3 -I tests/probes/voice_probe.py path/to/voice-note.ogg

It converts the ogg/opus file to wav with ffmpeg (list args, no shell), then tries three
request shapes, one call each, and prints what came back. The key is never printed.
A copy of the results goes to tests/probes/out/voice_probe.json (gitignored).

transcribe() in kit/tools/voice_note.py uses shape A today. If A fails and another
works, change only audio_message() in that file.
"""
import base64
import json
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from kit import env  # noqa: E402
from kit.model import Model, ModelError  # noqa: E402
from kit.tools.voice_note import MAX_AUDIO_BYTES, PROMPT, to_wav  # noqa: E402


def shapes(ogg, wav):
    b64wav = base64.b64encode(wav).decode("ascii")
    return {
        "A input_audio, wav (what transcribe() sends)": [
            {"type": "text", "text": PROMPT},
            {"type": "input_audio", "input_audio": {"data": b64wav, "format": "wav"}}],
        "B input_audio, the ogg as WhatsApp sent it": [
            {"type": "text", "text": PROMPT},
            {"type": "input_audio", "input_audio": {"data": base64.b64encode(ogg).decode("ascii"), "format": "ogg"}}],
        "C audio_url, wav as a data: URI": [
            {"type": "text", "text": PROMPT},
            {"type": "audio_url", "audio_url": {"url": "data:audio/wav;base64," + b64wav}}],
    }


def main(argv):
    if len(argv) != 2:
        raise SystemExit("usage: python3 -I tests/probes/voice_probe.py path/to/voice-note.ogg")
    src = Path(argv[1])
    if not src.is_file() or src.stat().st_size > MAX_AUDIO_BYTES:
        raise SystemExit("give a voice note file under 16 MB")
    env.load()
    if not env.get("BASETEN_API_KEY"):
        raise SystemExit("BASETEN_API_KEY is not set in .env")
    ogg = src.read_bytes()
    wav = to_wav(ogg)
    model = Model()
    print(f"model: {model.model_id}")
    print(f"voice note: {len(ogg)} bytes ogg, {len(wav)} bytes wav after ffmpeg\n")
    report = {"model": model.model_id, "at": time.strftime("%Y-%m-%d %H:%M"), "shapes": {}}
    for name, parts in shapes(ogg, wav).items():
        t0 = time.time()
        try:
            out = model.chat([{"role": "user", "content": parts}])
            text = (out.get("content") or "").strip()
            res = {"ok": bool(text), "seconds": round(time.time() - t0, 1), "text": text[:400]}
        except ModelError as e:
            res = {"ok": False, "seconds": round(time.time() - t0, 1), "error": str(e)[:400]}
        report["shapes"][name] = res
        print(f"{name}\n  {'WORKS' if res['ok'] else 'failed'} in {res['seconds']} s")
        print("  " + (res.get("text") or res.get("error") or "(empty reply)") + "\n")
    works = [n.split()[0] for n, r in report["shapes"].items() if r["ok"]]
    print("Works: " + (", ".join(works) if works else "none of the three"))
    out_dir = Path(__file__).parent / "out"
    out_dir.mkdir(exist_ok=True)
    (out_dir / "voice_probe.json").write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")


if __name__ == "__main__":
    main(sys.argv)
