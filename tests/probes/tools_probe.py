"""Live probe: function calling and photo input for Muse Spark Open on Baseten.

Run this yourself, with BASETEN_API_KEY in .env:

    python3 -I tests/probes/tools_probe.py [photo.jpg]

1. Function calling: a question that needs the store tool, sent with the real store
   definition and tool_choice "auto". Reports any tool_calls, the name and the arguments.
2. Round trip: sends the tool result back and reports the final reply.
3. Photo: sends a photo as an image_url part with a data: URI, the exact shape
   frontdesk/loop.py uses. Without a path it sends a small red square made here.
4. reasoning_effort: if Baseten refuses it, the probe retries once without it and says so.

The key is never printed. A copy goes to tests/probes/out/tools_probe.json (gitignored).
"""
import json
import struct
import sys
import time
import zlib
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from kit import env, media  # noqa: E402
from kit.model import Model, ModelError  # noqa: E402
from kit.tools import store  # noqa: E402


class NoEffort(Model):
    def body(self, *a, **kw):
        b = super().body(*a, **kw)
        b.pop("reasoning_effort", None)
        return b


def red_png(size=32):
    row = b"\x00" + b"\xff\x00\x00" * size
    raw = zlib.compress(row * size)

    def chunk(kind, data):
        return struct.pack(">I", len(data)) + kind + data + struct.pack(">I", zlib.crc32(kind + data))

    return (b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", struct.pack(">IIBBBBB", size, size, 8, 2, 0, 0, 0))
            + chunk(b"IDAT", raw) + chunk(b"IEND", b""))


def ask(model, messages, tools=None):
    t0 = time.time()
    try:
        return model.chat(messages, tools=tools), None, round(time.time() - t0, 1)
    except ModelError as e:
        return None, str(e)[:400], round(time.time() - t0, 1)


def main(argv):
    env.load()
    if not env.get("BASETEN_API_KEY"):
        raise SystemExit("BASETEN_API_KEY is not set in .env")
    model = Model()
    report = {"model": model.model_id, "at": time.strftime("%Y-%m-%d %H:%M")}
    print(f"model: {model.model_id}\n")

    # 1. function calling
    q = [{"role": "system", "content": "You are a store assistant. Use the store tool for any product fact."},
         {"role": "user", "content": "عندكم شراب اسود مقاس ٤٣؟ بكام؟"}]
    out, err, s = ask(model, q, [store.DEFINITION])
    if err and "reasoning" in err.lower():
        print("reasoning_effort was refused; retrying without it")
        report["reasoning_effort"] = "refused: " + err
        model = NoEffort()
        out, err, s = ask(model, q, [store.DEFINITION])
    else:
        report.setdefault("reasoning_effort", "accepted" if not err else "unknown, the call failed")
    calls = (out or {}).get("tool_calls") or []
    report["function_calling"] = {"seconds": s, "error": err, "tool_calls": calls, "content": (out or {}).get("content")}
    print("1. function calling:", "WORKS" if calls else "no tool call", f"({s} s)")
    for c in calls:
        print("   ", c.get("function", {}).get("name"), c.get("function", {}).get("arguments"))
    if err:
        print("   error:", err)

    # 2. round trip with the tool result
    if calls:
        c = calls[0]
        args = json.loads(c["function"].get("arguments") or "{}")
        result = store.run(args if store.check(args, None)[0] else {}, None)
        q2 = q + [{"role": "assistant", "content": out.get("content"), "tool_calls": calls}]
        q2 += [{"role": "tool", "tool_call_id": x.get("id", ""), "content": json.dumps(result, ensure_ascii=False)}
               for x in calls]
        out2, err2, s2 = ask(model, q2, [store.DEFINITION])
        report["round_trip"] = {"seconds": s2, "error": err2, "content": (out2 or {}).get("content")}
        print("2. round trip:", "WORKS" if out2 and out2.get("content") else "failed", f"({s2} s)")
        print("   ", (out2 or {}).get("content") or err2)

    # 3. photo
    if len(argv) > 1:
        data = Path(argv[1]).read_bytes()
        mime = media.sniff(data) or "image/jpeg"
        what = "Describe this photo in one line."
    else:
        data, mime, what = red_png(), "image/png", "What colour is this image? One word."
    ok, why = media.check_image(data, mime)
    if not ok:
        raise SystemExit(f"the photo fails our own check ({why}): jpeg, png or webp under 5 MB")
    shape = [{"type": "text", "text": what}, media.image_part(data, mime)]
    out3, err3, s3 = ask(model, [{"role": "user", "content": shape}])
    report["photo"] = {"mime": mime, "bytes": len(data), "seconds": s3, "error": err3,
                       "content": (out3 or {}).get("content")}
    print("3. photo:", "WORKS" if out3 and out3.get("content") else "failed", f"({s3} s)")
    print("   ", (out3 or {}).get("content") or err3)
    print("4. reasoning_effort:", report["reasoning_effort"])

    out_dir = Path(__file__).parent / "out"
    out_dir.mkdir(exist_ok=True)
    (out_dir / "tools_probe.json").write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")


if __name__ == "__main__":
    main(sys.argv)
