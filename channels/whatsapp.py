"""WhatsApp Cloud API channel: the webhook server and the Graph API client.

Run from the repo folder:  python3 -m channels.whatsapp
It listens on 127.0.0.1:PORT (default 8080). Meta must reach it through a public tunnel.

- GET  /webhook  the verify-token handshake.
- POST /webhook  the X-Hub-Signature-256 check runs on the raw bytes before anything
  parses the body (HMAC SHA-256 with the app secret, constant-time compare). Then text,
  voice notes and photos are parsed, deduped by message id (Meta retries), and queued.
  One worker thread answers them in order, so the webhook returns 200 at once.

Graph API version: GRAPH_API_VERSION, default v26.0, the latest in Meta's changelog
(https://developers.facebook.com/docs/graph-api/changelog, checked 28 Oct 2026; v26.0
released 29 Jul 2026). Message and media paths from the WhatsApp Cloud API reference:
POST /{version}/{phone_number_id}/messages and GET /{version}/{media_id}?phone_number_id=...
"""
import hashlib
import hmac
import json
import logging
import queue
import re
import threading
import urllib.parse
import urllib.request
from dataclasses import dataclass
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from channels.base import Channel, Image, Message
from kit import media

log = logging.getLogger("channels.whatsapp")

GRAPH = "https://graph.facebook.com"
GRAPH_VERSION_DEFAULT = "v26.0"
MAX_BODY = 1024 * 1024
MAX_AUDIO_BYTES = 16 * 1024 * 1024
MIN_SECRET = 32
SIGNATURE = re.compile(r"sha256=([0-9a-f]{64})")
CHALLENGE = re.compile(r"[0-9A-Za-z_-]{1,128}")


# signature

def sign(raw, secret):
    return "sha256=" + hmac.new(secret.encode("utf-8"), raw, hashlib.sha256).hexdigest()


def verify_signature(raw, header, secret):
    """True only if header is "sha256=<hex>" of HMAC SHA-256 over the raw body with the app secret."""
    if not secret or not isinstance(header, str):
        return False
    m = SIGNATURE.fullmatch(header)
    if not m:
        return False
    want = hmac.new(secret.encode("utf-8"), raw, hashlib.sha256).hexdigest()
    return hmac.compare_digest(want.encode("ascii"), m.group(1).encode("ascii"))


# parsing

@dataclass
class Incoming:
    """One WhatsApp message as parsed from the webhook, before any media is downloaded."""
    message_id: str
    customer_id: str
    timestamp: int
    kind: str                 # text, audio or image
    text: str = ""
    media_id: str = ""
    media_mime: str = ""


def _d(x):
    return x if isinstance(x, dict) else {}


def _l(x):
    return x if isinstance(x, list) else []


def parse(payload, phone_number_id=None):
    """Text, audio and image messages from a webhook payload. Other types and status updates are skipped."""
    out = []
    for entry in _l(_d(payload).get("entry")):
        for change in _l(_d(entry).get("changes")):
            value = _d(_d(change).get("value"))
            pnid = str(_d(value.get("metadata")).get("phone_number_id", ""))
            if phone_number_id and pnid != str(phone_number_id):
                continue
            for m in _l(value.get("messages")):
                m = _d(m)
                kind, mid, sender = m.get("type"), m.get("id"), m.get("from")
                if not (isinstance(mid, str) and mid and isinstance(sender, str) and sender):
                    continue
                try:
                    ts = int(m.get("timestamp", 0))
                except (TypeError, ValueError):
                    ts = 0
                inc = Incoming(mid, sender, ts, str(kind))
                if kind == "text":
                    inc.text = str(_d(m.get("text")).get("body", ""))
                elif kind in ("audio", "image"):
                    part = _d(m.get(kind))
                    inc.media_id = str(part.get("id", ""))
                    inc.media_mime = str(part.get("mime_type", ""))
                    inc.text = str(part.get("caption", "")) if kind == "image" else ""
                else:
                    continue
                out.append(inc)
    return out


# Graph API

class TooBig(Exception):
    pass


class Graph(Channel):
    """Sends replies and downloads media through the Graph API."""

    name = "whatsapp"

    def __init__(self, token, phone_number_id, version=GRAPH_VERSION_DEFAULT, opener=None, timeout=20):
        self._token = token
        self.phone_number_id = phone_number_id
        self.version = version
        self._open = opener or urllib.request.urlopen
        self.timeout = timeout

    def _req(self, url, data=None):
        headers = {"Authorization": "Bearer " + self._token}
        if data is not None:
            headers["Content-Type"] = "application/json"
            data = json.dumps(data).encode("utf-8")
        return urllib.request.Request(url, data=data, headers=headers, method="POST" if data else "GET")

    def send(self, to, text):
        url = f"{GRAPH}/{self.version}/{self.phone_number_id}/messages"
        body = {"messaging_product": "whatsapp", "recipient_type": "individual", "to": to,
                "type": "text", "text": {"preview_url": False, "body": text}}
        with self._open(self._req(url, body), timeout=self.timeout) as r:
            return json.loads(r.read().decode("utf-8"))

    def download(self, media_id, max_bytes):
        """(bytes, mime) for a media id. Raises TooBig past max_bytes without reading the rest."""
        if not re.fullmatch(r"[0-9A-Za-z_-]{1,128}", media_id or ""):
            raise ValueError("bad media id")
        q = urllib.parse.urlencode({"phone_number_id": self.phone_number_id})
        with self._open(self._req(f"{GRAPH}/{self.version}/{media_id}?{q}"), timeout=self.timeout) as r:
            info = json.loads(r.read().decode("utf-8"))
        if int(info.get("file_size") or 0) > max_bytes:
            raise TooBig(media_id)
        url = str(info.get("url", ""))
        if urllib.parse.urlsplit(url).scheme != "https":
            raise ValueError("media url is not https")
        with self._open(self._req(url), timeout=self.timeout) as r:
            data = r.read(max_bytes + 1)
        if len(data) > max_bytes:
            raise TooBig(media_id)
        return data, str(info.get("mime_type", ""))


def to_message(inc, graph):
    """Download what the message carries and build the channel-neutral Message."""
    msg = Message(channel="whatsapp", customer_id=inc.customer_id, text=inc.text,
                  message_id=inc.message_id, timestamp=inc.timestamp)
    if inc.kind == "audio":
        try:
            msg.audio, mime = graph.download(inc.media_id, MAX_AUDIO_BYTES)
            msg.audio_mime = mime or inc.media_mime
        except Exception:
            log.exception("voice note download failed")
    elif inc.kind == "image":
        declared = inc.media_mime.split(";")[0].strip().lower()
        if declared not in media.IMAGE_TYPES:
            msg.rejected_images += 1
            return msg
        try:
            data, mime = graph.download(inc.media_id, media.MAX_IMAGE_BYTES)
        except TooBig:
            msg.rejected_images += 1
            return msg
        except Exception:
            log.exception("photo download failed")
            msg.rejected_images += 1
            return msg
        mime = (mime or declared).split(";")[0].strip().lower()
        ok, _ = media.check_image(data, mime)
        if ok:
            msg.images.append(Image(data, mime))
        else:
            msg.rejected_images += 1
    return msg


# server

def make_handler(app_secret, verify_token, accept, phone_number_id=None):
    """The request handler. accept(list of Incoming) runs only for signed, parsed bodies."""
    if not app_secret or len(app_secret) < MIN_SECRET:
        raise ValueError("WHATSAPP_APP_SECRET is missing or too short")
    if not verify_token:
        raise ValueError("WHATSAPP_VERIFY_TOKEN is missing")

    class Handler(BaseHTTPRequestHandler):
        server_version = "ascent"
        sys_version = ""

        def _reply(self, code, text=""):
            body = text.encode("utf-8")
            self.send_response(code)
            self.send_header("Content-Type", "text/plain; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def do_GET(self):
            q = urllib.parse.parse_qs(urllib.parse.urlsplit(self.path).query)
            mode = q.get("hub.mode", [""])[0]
            token = q.get("hub.verify_token", [""])[0]
            challenge = q.get("hub.challenge", [""])[0]
            if (mode == "subscribe" and CHALLENGE.fullmatch(challenge)
                    and hmac.compare_digest(token.encode("utf-8"), verify_token.encode("utf-8"))):
                return self._reply(200, challenge)
            return self._reply(403, "forbidden")

        def do_POST(self):
            try:
                length = int(self.headers.get("Content-Length", ""))
            except ValueError:
                return self._reply(411, "length required")
            if length < 0 or length > MAX_BODY:
                self.close_connection = True
                return self._reply(413, "too large")
            raw = self.rfile.read(length)
            if not verify_signature(raw, self.headers.get("X-Hub-Signature-256"), app_secret):
                return self._reply(403, "bad signature")
            try:
                payload = json.loads(raw)
            except ValueError:
                return self._reply(400, "bad json")
            try:
                accept(parse(payload, phone_number_id))
            except Exception:
                log.exception("could not queue messages")
                return self._reply(500, "error")
            return self._reply(200, "ok")

        def log_message(self, fmt, *args):
            # The default log prints the full request line, and the GET query holds the verify token.
            log.info("%s %s", self.command, urllib.parse.urlsplit(self.path).path)

    return Handler


class App:
    """Dedupes and queues incoming messages, and answers them one at a time."""

    def __init__(self, chats, model, lessons, results, graph):
        self.chats, self.model, self.lessons, self.results, self.graph = chats, model, lessons, results, graph
        self.q = queue.Queue()

    def accept(self, items):
        for inc in items:
            if self.chats.first_seen(inc.message_id):
                self.q.put(inc)

    def work_one(self, inc):
        from frontdesk import loop
        msg = to_message(inc, self.graph)
        return loop.turn(msg, chats=self.chats, model=self.model, lessons=self.lessons,
                         results=self.results, send=self.graph.send)

    def work(self):
        while True:
            inc = self.q.get()
            if inc is None:
                return
            try:
                self.work_one(inc)
            except Exception:
                log.exception("turn failed")


def main():
    from kit import env
    from kit.chats import Chats
    from kit.db import Db
    from kit.model import Model
    from learning.lessons import Lessons
    from learning.results import Results

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(message)s")
    env.load()
    need = ["WHATSAPP_APP_SECRET", "WHATSAPP_VERIFY_TOKEN", "WHATSAPP_TOKEN", "WHATSAPP_PHONE_NUMBER_ID",
            "BASETEN_API_KEY"]
    missing = [n for n in need if not env.get(n)]
    if missing:
        raise SystemExit("Missing in .env: " + ", ".join(missing))
    db = Db()
    pnid = env.get("WHATSAPP_PHONE_NUMBER_ID")
    graph = Graph(env.get("WHATSAPP_TOKEN"), pnid, env.get("GRAPH_API_VERSION", GRAPH_VERSION_DEFAULT))
    app = App(Chats(db), Model(), Lessons(db), Results(db), graph)
    handler = make_handler(env.get("WHATSAPP_APP_SECRET"), env.get("WHATSAPP_VERIFY_TOKEN"), app.accept, pnid)
    port = int(env.get("PORT", "8080"))
    server = ThreadingHTTPServer(("127.0.0.1", port), handler)
    threading.Thread(target=app.work, daemon=True).start()
    log.info("listening on 127.0.0.1:%d", port)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        app.q.put(None)
        server.server_close()


if __name__ == "__main__":
    main()
