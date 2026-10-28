import contextlib
import http.client
import io
import json
import os
import secrets
import socket
import threading
import time
import unittest
import urllib.error
import urllib.parse
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from unittest import mock

from channels import whatsapp as wa
from kit import limits as limits_mod
from kit import media
from kit.chats import Chats
from kit.db import Db
from kit.limits import Limits

# Test values are made fresh on every run; none is a real key.
APP = secrets.token_hex(20)
OTHER = secrets.token_hex(20)
VERIFY = secrets.token_urlsafe(12)
BEARER = secrets.token_urlsafe(12)
JPEG = b"\xff\xd8\xff\xe0" + b"\x00" * 200
GIF = b"GIF89a" + b"\x00" * 200


def payload(*messages, pnid="111"):
    return {"object": "whatsapp_business_account", "entry": [{"id": "1", "changes": [{"field": "messages", "value": {
        "messaging_product": "whatsapp",
        "metadata": {"display_phone_number": "201000000000", "phone_number_id": pnid},
        "messages": list(messages)}}]}]}


def text_msg(mid, body, frm="201111111111"):
    return {"from": frm, "id": mid, "timestamp": "1761600000", "type": "text", "text": {"body": body}}


def image_msg(mid, caption="", mime="image/jpeg"):
    return {"from": "201111111111", "id": mid, "timestamp": "1761600000", "type": "image",
            "image": {"id": "MEDIA1", "mime_type": mime, "caption": caption}}


def audio_msg(mid):
    return {"from": "201111111111", "id": mid, "timestamp": "1761600000", "type": "audio",
            "audio": {"id": "MEDIA2", "mime_type": "audio/ogg; codecs=opus", "voice": True}}


class SignatureCheck(unittest.TestCase):
    body = json.dumps(payload(text_msg("wamid.1", "hi"))).encode()

    def test_good(self):
        self.assertTrue(wa.verify_signature(self.body, wa.sign(self.body, APP), APP))

    def test_bad(self):
        self.assertFalse(wa.verify_signature(self.body, wa.sign(self.body, OTHER), APP))

    def test_missing(self):
        self.assertFalse(wa.verify_signature(self.body, None, APP))
        self.assertFalse(wa.verify_signature(self.body, "", APP))

    def test_tampered_body(self):
        sig = wa.sign(self.body, APP)
        self.assertFalse(wa.verify_signature(self.body.replace(b"hi", b"ho"), sig, APP))

    def test_malformed_header(self):
        hexsig = wa.sign(self.body, APP).split("=", 1)[1]
        for bad in (hexsig, "sha1=" + hexsig, "sha256=" + hexsig.upper(), "sha256=" + hexsig + " ", "sha256=zz"):
            self.assertFalse(wa.verify_signature(self.body, bad, APP), bad)

    def test_no_app_secret(self):
        self.assertFalse(wa.verify_signature(self.body, wa.sign(self.body, ""), ""))


class WebhookServer(unittest.TestCase):
    def setUp(self):
        self.got = []
        handler = wa.make_handler(APP, VERIFY, self.got.extend, "111")
        self.srv = ThreadingHTTPServer(("127.0.0.1", 0), handler)
        self.thread = threading.Thread(target=self.srv.serve_forever, daemon=True)
        self.thread.start()
        self.url = f"http://127.0.0.1:{self.srv.server_address[1]}/webhook"

    def tearDown(self):
        self.srv.shutdown()
        self.srv.server_close()
        self.thread.join(5)

    def request(self, raw=None, sig=None, query=None):
        headers = {"Content-Type": "application/json"}
        if sig is not None:
            headers["X-Hub-Signature-256"] = sig
        url = self.url + ("?" + urllib.parse.urlencode(query) if query else "")
        req = urllib.request.Request(url, data=raw, headers=headers, method="POST" if raw is not None else "GET")
        try:
            with urllib.request.urlopen(req, timeout=5) as r:
                return r.status, r.read().decode()
        except urllib.error.HTTPError as e:
            with e:
                return e.code, e.read().decode()

    def test_unsigned_is_rejected_before_parsing(self):
        self.assertEqual(self.request(b"not json", None)[0], 403)
        self.assertEqual(self.request(b"not json", "sha256=" + "0" * 64)[0], 403)
        # the same broken body, signed, gets as far as the parser: the signature ran first
        self.assertEqual(self.request(b"not json", wa.sign(b"not json", APP))[0], 400)
        self.assertEqual(self.got, [])

    def test_signed_message_is_accepted(self):
        raw = json.dumps(payload(text_msg("wamid.1", "عندكم شرابات مقاس ٤٣؟"))).encode()
        self.assertEqual(self.request(raw, wa.sign(raw, APP)), (200, "ok"))
        self.assertEqual([(i.message_id, i.text) for i in self.got], [("wamid.1", "عندكم شرابات مقاس ٤٣؟")])

    def test_tampered_body_is_rejected(self):
        raw = json.dumps(payload(text_msg("wamid.1", "hi"))).encode()
        sig = wa.sign(raw, APP)
        self.assertEqual(self.request(raw.replace(b"hi", b"yo"), sig)[0], 403)
        self.assertEqual(self.got, [])

    def test_other_phone_number_is_skipped(self):
        raw = json.dumps(payload(text_msg("wamid.1", "hi"), pnid="999")).encode()
        self.assertEqual(self.request(raw, wa.sign(raw, APP))[0], 200)
        self.assertEqual(self.got, [])

    def test_too_large_is_refused_unread(self):
        conn = http.client.HTTPConnection("127.0.0.1", self.srv.server_address[1], timeout=5)
        conn.putrequest("POST", "/webhook")
        conn.putheader("Content-Length", str(wa.MAX_BODY + 1))
        conn.endheaders()
        self.assertEqual(conn.getresponse().status, 413)
        conn.close()
        self.assertEqual(self.got, [])

    def test_handshake(self):
        good = {"hub.mode": "subscribe", "hub.verify_token": VERIFY, "hub.challenge": "1158201444"}
        self.assertEqual(self.request(query=good), (200, "1158201444"))
        self.assertEqual(self.request(query={**good, "hub.verify_token": VERIFY + "x"})[0], 403)
        self.assertEqual(self.request(query={**good, "hub.mode": "unsubscribe"})[0], 403)
        self.assertEqual(self.request(query={**good, "hub.challenge": "<b>"})[0], 403)

    def test_short_app_secret_refused(self):
        with self.assertRaises(ValueError):
            wa.make_handler("short", VERIFY, print)

    def test_negative_length_is_refused_unread(self):
        # Content-Length -1 would make rfile.read(-1) wait for the client to close
        s = socket.create_connection(("127.0.0.1", self.srv.server_address[1]), timeout=5)
        self.addCleanup(s.close)
        s.sendall(b"POST /webhook HTTP/1.1\r\nHost: x\r\nContent-Length: -1\r\n\r\n{}")
        s.shutdown(socket.SHUT_WR)
        self.assertEqual(s.makefile("rb").readline().split(b" ")[1], b"413")
        self.assertEqual(self.got, [])

    def test_request_log_never_holds_the_verify_token(self):
        err = io.StringIO()
        good = {"hub.mode": "subscribe", "hub.verify_token": VERIFY, "hub.challenge": "1158201444"}
        with self.assertLogs("channels.whatsapp", "INFO") as logs, contextlib.redirect_stderr(err):
            self.assertEqual(self.request(query=good)[0], 200)
            self.assertEqual(self.request(query={**good, "hub.verify_token": VERIFY + "x"})[0], 403)
        text = "\n".join(logs.output) + err.getvalue()
        self.assertIn("GET /webhook", text)
        self.assertNotIn(VERIFY, text)


class StalledBody(unittest.TestCase):
    def test_default_socket_timeout_is_ten_seconds(self):
        self.assertEqual(wa.make_handler(APP, VERIFY, print).timeout, 10)

    def test_stalled_body_is_dropped(self):
        srv = ThreadingHTTPServer(("127.0.0.1", 0), wa.make_handler(APP, VERIFY, print, timeout=1))
        thread = threading.Thread(target=srv.serve_forever, daemon=True)
        thread.start()
        self.addCleanup(thread.join, 5)
        self.addCleanup(srv.server_close)
        self.addCleanup(srv.shutdown)
        s = socket.create_connection(("127.0.0.1", srv.server_address[1]), timeout=4)
        self.addCleanup(s.close)
        start = time.monotonic()
        with self.assertLogs("channels.whatsapp", "INFO"):
            s.sendall(b"POST /webhook HTTP/1.1\r\nHost: x\r\nContent-Length: 100\r\n\r\n")
            self.assertEqual(s.recv(1024), b"")  # closed with no reply
        self.assertLess(time.monotonic() - start, 3)

class Parsing(unittest.TestCase):
    def test_text_audio_image(self):
        items = wa.parse(payload(text_msg("a", "hi"), audio_msg("b"), image_msg("c", caption="3ndko de?"),
                                 {"from": "2011", "id": "d", "type": "sticker", "sticker": {}}), "111")
        self.assertEqual([i.kind for i in items], ["text", "audio", "image"])
        self.assertEqual(items[1].media_id, "MEDIA2")
        self.assertEqual((items[2].media_id, items[2].media_mime, items[2].text), ("MEDIA1", "image/jpeg", "3ndko de?"))
        self.assertEqual(items[0].timestamp, 1761600000)

    def test_junk_is_ignored(self):
        self.assertEqual(wa.parse({"entry": "x"}), [])
        self.assertEqual(wa.parse([1, 2]), [])
        self.assertEqual(wa.parse(payload({"type": "text"})), [])

    def test_retries_are_deduped(self):
        app = wa.App(Chats(Db(":memory:")), None, None, None, None)
        self.addCleanup(app.chats.db.close)
        items = wa.parse(payload(text_msg("wamid.same", "hi")))
        app.accept(items)
        app.accept(items)
        self.assertEqual(app.q.qsize(), 1)


class FakeResp:
    def __init__(self, data):
        self.data, self.reads = data, []

    def read(self, n=-1):
        self.reads.append(n)
        return self.data if n is None or n < 0 else self.data[:n]

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


class FakeGraph:
    def __init__(self, data=JPEG, mime="image/jpeg", error=None):
        self.data, self.mime, self.error, self.calls, self.caps = data, mime, error, 0, []

    def download(self, media_id, max_bytes):
        self.calls += 1
        self.caps.append(max_bytes)
        if self.error:
            raise self.error
        return self.data, self.mime


class Photos(unittest.TestCase):
    def inc(self, mime="image/jpeg", caption=""):
        return wa.parse(payload(image_msg("img", caption=caption, mime=mime)))[0]

    def test_photo_is_kept(self):
        msg = wa.to_message(self.inc(caption="3ndko de?"), FakeGraph())
        self.assertEqual((len(msg.images), msg.rejected_images, msg.text), (1, 0, "3ndko de?"))
        self.assertEqual(msg.images[0].mime, "image/jpeg")

    def test_wrong_declared_type_is_rejected_without_download(self):
        g = FakeGraph(GIF, "image/gif")
        msg = wa.to_message(self.inc(mime="image/gif"), g)
        self.assertEqual((len(msg.images), msg.rejected_images, g.calls), (0, 1, 0))

    def test_bytes_must_match_the_type(self):
        msg = wa.to_message(self.inc(), FakeGraph(GIF, "image/jpeg"))
        self.assertEqual((len(msg.images), msg.rejected_images), (0, 1))

    def test_oversize_is_rejected(self):
        msg = wa.to_message(self.inc(), FakeGraph(error=wa.TooBig("img")))
        self.assertEqual((len(msg.images), msg.rejected_images), (0, 1))
        big = b"\xff\xd8\xff" + b"\x00" * media.MAX_IMAGE_BYTES
        self.assertEqual(media.check_image(big, "image/jpeg"), (False, "size"))

    def test_graph_download_stops_at_the_cap(self):
        seen = []

        def opener(req, timeout):
            seen.append(req)
            if len(seen) == 1:
                return FakeResp(json.dumps({"url": "https://lookaside.fbsbx.com/x", "mime_type": "image/jpeg",
                                            "file_size": 10}).encode())
            return FakeResp(b"\xff\xd8\xff" + b"\x00" * 100)

        reads = []

        def recording(req, timeout):
            r = opener(req, timeout)
            reads.append(r.reads)
            return r

        g = wa.Graph(BEARER, "111", opener=recording)
        with self.assertRaises(wa.TooBig):
            g.download("123", max_bytes=50)
        self.assertEqual(seen[0].full_url, "https://graph.facebook.com/v26.0/123?phone_number_id=111")
        self.assertEqual(seen[1].get_header("Authorization"), "Bearer " + BEARER)
        # the media body is read with a cap of max_bytes + 1, never in full
        self.assertEqual(reads[1], [51])

    def test_graph_refuses_declared_oversize(self):
        calls = []

        def opener(req, timeout):
            calls.append(req)
            return FakeResp(json.dumps({"url": "https://x", "file_size": 999}).encode())

        with self.assertRaises(wa.TooBig):
            wa.Graph(BEARER, "111", opener=opener).download("123", max_bytes=50)
        self.assertEqual(len(calls), 1)

    def test_send_shape(self):
        seen = []

        def opener(req, timeout):
            seen.append(req)
            return FakeResp(b'{"messages":[{"id":"wamid.out"}]}')

        wa.Graph(BEARER, "111", opener=opener).send("201111111111", "تمام")
        body = json.loads(seen[0].data)
        self.assertEqual(seen[0].full_url, "https://graph.facebook.com/v26.0/111/messages")
        self.assertEqual((body["to"], body["type"], body["text"]["body"]), ("201111111111", "text", "تمام"))


OGG = b"OggS" + b"\x00" * 60


class MediaPinning(unittest.TestCase):
    def graph(self, url):
        """A Graph whose media info points at url. Returns (requests made, graph)."""
        calls = []

        def opener(req, timeout):
            calls.append(req)
            if len(calls) == 1:
                return FakeResp(json.dumps({"url": url, "mime_type": "audio/ogg", "file_size": 10}).encode())
            return FakeResp(OGG)

        return calls, wa.Graph(BEARER, "111", opener=opener)

    def test_media_url_must_be_https_on_metas_host(self):
        for url in ("http://lookaside.fbsbx.com/x", "ftp://lookaside.fbsbx.com/x", "https://evil.example/x",
                    "https://lookaside.fbsbx.com.evil.example/x", "https://evil.example/lookaside.fbsbx.com",
                    "https://user@lookaside.fbsbx.com/x", "https://user:pw@lookaside.fbsbx.com/x",
                    "https://lookaside.fbsbx.com:8443/x", "https://lookaside.fbsbx.com:80/x",
                    "https://lookaside.fbsbx.com:abc/x", "//lookaside.fbsbx.com/x", ""):
            with self.subTest(url=url):
                calls, g = self.graph(url)
                with self.assertRaises(ValueError):
                    g.download("123", 100)
                self.assertEqual(len(calls), 1)  # the token never went to the media url

    def test_metas_media_host_is_fetched(self):
        for url in ("https://lookaside.fbsbx.com/whatsapp_business/attachments/?mid=1",
                    "https://lookaside.fbsbx.com:443/x"):
            with self.subTest(url=url):
                calls, g = self.graph(url)
                self.assertEqual(g.download("123", 100), (OGG, "audio/ogg"))
                self.assertEqual(calls[1].full_url, url)

    def test_media_id_shape(self):
        for bad in ("../x", "a/b", "1?x=2", "", "x" * 129, "١٢٣", None):
            with self.subTest(media_id=bad):
                calls = []
                g = wa.Graph(BEARER, "111", opener=lambda req, timeout: calls.append(req))
                with self.assertRaises(ValueError):
                    g.download(bad, 100)
                self.assertEqual(calls, [])

    def test_graph_opener_refuses_redirects(self):
        director = wa.Graph(BEARER, "111")._open.__self__
        self.assertIsInstance(director, urllib.request.OpenerDirector)
        self.assertTrue(any(isinstance(h, wa.NoRedirects) for h in director.handlers))
        self.assertFalse(any(type(h) is urllib.request.HTTPRedirectHandler for h in director.handlers))

    def test_redirect_is_not_followed(self):
        hits = []

        class Redirecting(BaseHTTPRequestHandler):
            def do_GET(self):
                hits.append(self.path)
                self.send_response(302 if self.path == "/a" else 200)
                if self.path == "/a":
                    self.send_header("Location", "/b")
                self.send_header("Content-Length", "0")
                self.end_headers()

            def log_message(self, *args):
                pass

        srv = ThreadingHTTPServer(("127.0.0.1", 0), Redirecting)
        thread = threading.Thread(target=srv.serve_forever, daemon=True)
        thread.start()
        self.addCleanup(thread.join, 5)
        self.addCleanup(srv.server_close)
        self.addCleanup(srv.shutdown)
        req = urllib.request.Request(f"http://127.0.0.1:{srv.server_address[1]}/a",
                                     headers={"Authorization": "Bearer " + BEARER})
        with self.assertRaises(urllib.error.HTTPError) as cm:
            wa.no_redirect_opener().open(req, timeout=5)
        cm.exception.close()
        self.assertEqual((cm.exception.code, hits), (302, ["/a"]))


class VoiceNotes(unittest.TestCase):
    def inc(self, mime="audio/ogg; codecs=opus"):
        m = audio_msg("v1")
        m["audio"]["mime_type"] = mime
        return wa.parse(payload(m))[0]

    def test_voice_note_is_fetched_with_the_audio_cap(self):
        g = FakeGraph(OGG, "audio/ogg; codecs=opus")
        msg = wa.to_message(self.inc(), g)
        self.assertEqual((msg.audio, msg.audio_failed), (OGG, False))
        self.assertEqual(g.caps, [16 * 1024 * 1024])

    def test_voice_must_be_audio_ogg(self):
        g = FakeGraph(OGG, "audio/ogg")
        msg = wa.to_message(self.inc("audio/mpeg"), g)
        self.assertEqual((msg.audio_failed, msg.audio, g.calls), (True, None, 0))  # refused before the download
        for data, mime in ((OGG, "audio/mpeg"), (OGG, "video/ogg"), (b"", "audio/ogg")):
            with self.subTest(mime=mime, size=len(data)):
                msg = wa.to_message(self.inc(), FakeGraph(data, mime))
                self.assertEqual((msg.audio_failed, msg.audio), (True, None))

    def test_failed_voice_download(self):
        for err in (wa.TooBig("v1"), ValueError("media url is not on Meta's media host"), OSError("down")):
            with self.subTest(err=type(err).__name__), self.assertLogs("channels.whatsapp", "ERROR"):
                msg = wa.to_message(self.inc(), FakeGraph(error=err))
                self.assertEqual((msg.audio_failed, msg.audio), (True, None))


class Queueing(unittest.TestCase):
    def setUp(self):
        self.db = Db(":memory:")
        self.addCleanup(self.db.close)
        self.chats = Chats(self.db)

    def app(self, **kw):
        return wa.App(self.chats, None, None, None, None, **kw)

    def test_default_queue_size(self):
        self.assertEqual((wa.MAX_QUEUE, self.app().q.maxsize), (200, 200))

    def test_full_queue_is_busy_and_forgets_the_id(self):
        app = self.app(maxsize=1)
        app.accept(wa.parse(payload(text_msg("m1", "hi"))))
        with self.assertRaises(wa.Busy):
            app.accept(wa.parse(payload(text_msg("m2", "hi again"))))
        self.assertEqual(app.q.qsize(), 1)
        self.assertFalse(self.chats.first_seen("m1"))  # kept
        self.assertTrue(self.chats.first_seen("m2"))   # forgotten, so Meta's retry is taken

    def test_webhook_answers_503_when_full(self):
        app = self.app(maxsize=1)
        srv = ThreadingHTTPServer(("127.0.0.1", 0), wa.make_handler(APP, VERIFY, app.accept, "111"))
        thread = threading.Thread(target=srv.serve_forever, daemon=True)
        thread.start()
        self.addCleanup(thread.join, 5)
        self.addCleanup(srv.server_close)
        self.addCleanup(srv.shutdown)

        def post(mid):
            raw = json.dumps(payload(text_msg(mid, "hi"))).encode()
            req = urllib.request.Request(f"http://127.0.0.1:{srv.server_address[1]}/webhook", data=raw,
                                         headers={"X-Hub-Signature-256": wa.sign(raw, APP)}, method="POST")
            try:
                with urllib.request.urlopen(req, timeout=5) as r:
                    return r.status
            except urllib.error.HTTPError as e:
                with e:
                    return e.code

        self.assertEqual(post("m1"), 200)
        with self.assertLogs("channels.whatsapp", "WARNING"):
            self.assertEqual(post("m2"), 503)
        app.q.get_nowait()
        self.assertEqual(post("m2"), 200)  # Meta's retry is taken once there is room
        self.assertEqual(app.q.get_nowait()[1].message_id, "m2")

    def test_accept_marks_customers_over_a_limit(self):
        limits = Limits(self.db, per_customer=1, window=600, daily=3)
        limits.take(limits_mod.key("whatsapp", "201111111111"))
        app = self.app(limits=limits)
        app.accept(wa.parse(payload(text_msg("m1", "hi"), text_msg("m2", "hi", frm="202222222222"))))
        items = [app.q.get_nowait() for _ in range(2)]
        self.assertEqual([(over, inc.message_id) for over, inc in items], [("customer", "m1"), (None, "m2")])
        limits.take(limits_mod.key("whatsapp", "203333333333"))
        limits.take(limits_mod.key("whatsapp", "204444444444"))
        app.accept(wa.parse(payload(text_msg("m3", "hi", frm="202222222222"))))
        self.assertEqual(app.q.get_nowait()[0], "daily")

    def test_pending_messages_are_requeued_at_start(self):
        self.app().accept(wa.parse(payload(text_msg("m1", "hi"))))
        app = self.app()  # the process stopped before the turn was saved
        self.assertEqual(app.requeue_pending(), 1)
        over, inc = app.q.get_nowait()
        self.assertEqual((over, inc.message_id, inc.text, inc.kind), (None, "m1", "hi", "text"))


class RequiredEnv(unittest.TestCase):
    FULL = {n: "set" for n in wa.REQUIRED_ENV}

    def test_operator_number_is_required(self):
        self.assertIn("OPERATOR_WHATSAPP", wa.REQUIRED_ENV)
        with mock.patch.dict(os.environ, self.FULL, clear=True):
            self.assertEqual(wa.missing_env(), [])
        for env in ({**self.FULL, "OPERATOR_WHATSAPP": " "},
                    {k: v for k, v in self.FULL.items() if k != "OPERATOR_WHATSAPP"}):
            with mock.patch.dict(os.environ, env, clear=True):
                self.assertEqual(wa.missing_env(), ["OPERATOR_WHATSAPP"])

    def test_server_does_not_start_without_it(self):
        env = {k: v for k, v in self.FULL.items() if k != "OPERATOR_WHATSAPP"}
        with mock.patch.dict(os.environ, env, clear=True), mock.patch("kit.env.load", return_value=[]), \
                mock.patch("logging.basicConfig"), mock.patch.object(wa, "ThreadingHTTPServer") as server:
            with self.assertRaises(SystemExit) as cm:
                wa.main()
        self.assertIn("OPERATOR_WHATSAPP", str(cm.exception.code))
        server.assert_not_called()

    def test_start_checks_prices_against_size_numbers(self):
        class Stop(Exception):
            pass

        with mock.patch.dict(os.environ, self.FULL, clear=True), mock.patch("kit.env.load", return_value=[]), \
                mock.patch("logging.basicConfig"), mock.patch.object(wa, "ThreadingHTTPServer") as server, \
                mock.patch("frontdesk.loop.warn_size_prices", side_effect=Stop) as warn, \
                mock.patch("kit.db.Db") as db:
            with self.assertRaises(Stop):
                wa.main()
        warn.assert_called_once_with()
        db.assert_not_called()  # the check runs right after the env check, before the database opens
        server.assert_not_called()


if __name__ == "__main__":
    unittest.main()
