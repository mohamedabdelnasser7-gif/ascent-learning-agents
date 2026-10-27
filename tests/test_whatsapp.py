import http.client
import json
import secrets
import threading
import unittest
import urllib.error
import urllib.parse
import urllib.request
from http.server import ThreadingHTTPServer

from channels import whatsapp as wa
from kit import media
from kit.chats import Chats
from kit.db import Db

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
        self.data = data

    def read(self, n=-1):
        return self.data if n is None or n < 0 else self.data[:n]

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


class FakeGraph:
    def __init__(self, data=JPEG, mime="image/jpeg", error=None):
        self.data, self.mime, self.error, self.calls = data, mime, error, 0

    def download(self, media_id, max_bytes):
        self.calls += 1
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

        g = wa.Graph(BEARER, "111", opener=opener)
        with self.assertRaises(wa.TooBig):
            g.download("123", max_bytes=50)
        self.assertEqual(seen[0].full_url, "https://graph.facebook.com/v26.0/123?phone_number_id=111")
        self.assertEqual(seen[1].get_header("Authorization"), "Bearer " + BEARER)

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


if __name__ == "__main__":
    unittest.main()
