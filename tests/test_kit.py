import io
import json
import os
import secrets
import sqlite3
import tempfile
import unittest
import urllib.error
from pathlib import Path
from unittest import mock

from kit import env
from kit.chats import Chats
from kit.db import Db
from kit.model import DEFAULT_MODEL, EFFORT_REPLY, EFFORT_STUDY, ENDPOINT, Model, ModelError
from kit.tools import store
from learning.lessons import Lessons
from learning.results import Results
from tests.fake_model import clean_env


class ChatsTest(unittest.TestCase):
    def setUp(self):
        self.chats = Chats(Db(":memory:"))
        self.addCleanup(self.chats.db.close)
        self.cid = self.chats.open("whatsapp", "201111111111")
        c = self.chats
        c.add_step(self.cid, "user", content="عندكم شرابات مقاس ٤٣؟")                       # 1
        c.add_step(self.cid, "assistant", tool_calls=[{"id": "c1", "type": "function",
                   "function": {"name": "store", "arguments": "{}"}}])                       # 2
        c.add_step(self.cid, "tool", content='{"ok": true}', tool_call_id="c1", name="store")  # 3
        c.add_step(self.cid, "assistant", content="أيوه عندنا.")                             # 4
        c.add_step(self.cid, "user", content="عايز اطلب")                                    # 5
        c.set_stage(self.cid, "ordering")                                                    # 6 (event)
        c.add_failed_check(self.cid, "bad price")                                            # 7 (event)
        c.add_step(self.cid, "assistant", content="تمام.")                                   # 8

    def test_open_resumes_the_same_chat(self):
        self.assertEqual(self.chats.open("whatsapp", "201111111111"), self.cid)
        self.assertNotEqual(self.chats.open("whatsapp", "201222222222"), self.cid)

    def test_branch_copies_up_to_the_step_and_leaves_the_original(self):
        before = self.chats.steps(self.cid)
        b = self.chats.branch(self.cid, 4)
        self.assertEqual([s["n"] for s in self.chats.steps(b)], [1, 2, 3, 4])
        self.assertEqual([s["content"] for s in self.chats.steps(b)], [s["content"] for s in before[:4]])
        info = self.chats.get(b)
        self.assertEqual((info["stage"], info["failed_checks"], info["parent_id"], info["branched_at"]),
                         ("asking", 0, self.cid, 4))
        # the copy can go its own way
        self.chats.add_step(b, "assistant", content="different")
        self.chats.set_stage(b, "my_order")
        self.assertEqual(self.chats.steps(self.cid), before)
        self.assertEqual(self.chats.stage(self.cid), "ordering")
        # opening the customer's chat never lands on a branch
        self.assertEqual(self.chats.open("whatsapp", "201111111111"), self.cid)

    def test_branch_rebuilds_stage_and_failures(self):
        b = self.chats.branch(self.cid, 7)
        self.assertEqual((self.chats.stage(b), self.chats.failed_checks(b)), ("ordering", 1))

    def test_messages_resume_without_events(self):
        msgs = self.chats.messages(self.cid)
        self.assertEqual([m["role"] for m in msgs], ["user", "assistant", "tool", "assistant", "user", "assistant"])
        self.assertEqual(msgs[2]["tool_call_id"], "c1")
        # trimming never starts on a cut-off tool reply
        self.assertEqual(self.chats.messages(self.cid, last=4)[0]["role"], "user")

    def test_media_shows_as_a_note(self):
        self.chats.add_step(self.cid, "user", content="de?", media=[{"kind": "image", "sha256": "ab", "mime": "image/png"}])
        self.assertIn("photo", self.chats.messages(self.cid)[-1]["content"])

    def test_first_seen(self):
        self.assertTrue(self.chats.first_seen("wamid.1"))
        self.assertFalse(self.chats.first_seen("wamid.1"))


class LessonsTest(unittest.TestCase):
    def setUp(self):
        self.db = Db(":memory:")
        self.addCleanup(self.db.close)
        self.lessons = Lessons(self.db)

    def test_starts_empty(self):
        self.assertEqual(self.lessons.pick("asking", "anything"), [])

    def test_pick_fits_the_stage_and_caps_at_8(self):
        for i in range(10):
            self.lessons.add(f"asking lesson {i}", ["asking"])
        self.lessons.add("about late orders and delivery times", ["my_order"])
        self.lessons.add("never say الجوز", ["all"])
        picked = self.lessons.pick("asking", "price")
        self.assertEqual(len(picked), 8)
        mine = self.lessons.pick("my_order", "my order is late")
        self.assertEqual([p["text"] for p in mine], ["about late orders and delivery times", "never say الجوز"])
        self.assertNotIn("about late orders and delivery times", [p["text"] for p in self.lessons.pick("asking", "late")])

    def test_words_in_common_come_first(self):
        self.lessons.add("give delivery times for late orders", ["asking"])
        self.lessons.add("say the price plainly", ["asking"])
        self.assertEqual(self.lessons.pick("asking", "what is the price?")[0]["text"], "say the price plainly")

    def test_never_rewritten_only_added_or_retired(self):
        lid = self.lessons.add("say the price plainly", ["asking"])
        with self.assertRaises(sqlite3.DatabaseError):
            with self.db.tx() as c:
                c.execute("UPDATE lessons SET text = 'something else' WHERE id = ?", (lid,))
        with self.assertRaises(sqlite3.DatabaseError):
            with self.db.tx() as c:
                c.execute("UPDATE lessons SET stages = ',all,' WHERE id = ?", (lid,))
        self.assertTrue(self.lessons.retire(lid, "replaced"))
        self.assertEqual(self.lessons.pick("asking", "price"), [])
        with self.assertRaises(sqlite3.DatabaseError):
            with self.db.tx() as c:
                c.execute("UPDATE lessons SET status = 'active' WHERE id = ?", (lid,))
        self.lessons.set_score(lid, 2.5)  # the score may move

    def test_one_line_only(self):
        for bad in ("two\nlines", "", "x" * 301):
            with self.assertRaises(ValueError):
                self.lessons.add(bad, ["asking"])
        with self.assertRaises(ValueError):
            self.lessons.add("ok", [])


class ResultsTest(unittest.TestCase):
    def test_record(self):
        r = Results(Db(":memory:"))
        self.addCleanup(r.db.close)
        r.record("c1", "replied", step=4, stage="asking", lessons=[1, 2])
        r.record("c1", "check_failed", detail={"feedback": "x"})
        self.assertEqual([x["outcome"] for x in r.for_chat("c1")], ["replied", "check_failed"])
        self.assertEqual(r.for_chat("c1")[0]["lessons"], [1, 2])
        with self.assertRaises(ValueError):
            r.record("c1", "made_up")


class FakeResp:
    def __init__(self, data):
        self.data = data

    def read(self, n=-1):
        return self.data

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


class ModelTest(unittest.TestCase):
    def setUp(self):
        clean_env(self)
        self.key = secrets.token_urlsafe(16)

    def test_request_shape(self):
        seen = []

        def opener(req, timeout):
            seen.append(req)
            return FakeResp(json.dumps({"choices": [{"message": {"role": "assistant", "content": "hi"}}]}).encode())

        m = Model(api_key=self.key, opener=opener)
        out = m.chat([{"role": "user", "content": "hi"}], tools=[{"type": "function", "function": {"name": "store"}}])
        self.assertEqual(out["content"], "hi")
        body = json.loads(seen[0].data)
        self.assertEqual(seen[0].full_url, ENDPOINT)
        self.assertEqual(seen[0].get_header("Authorization"), "Bearer " + self.key)
        self.assertEqual((body["model"], body["reasoning_effort"], body["tool_choice"]), (DEFAULT_MODEL, "medium", "auto"))
        self.assertEqual((EFFORT_REPLY, EFFORT_STUDY), ("medium", "high"))
        self.assertEqual(m.body([], effort=EFFORT_STUDY)["reasoning_effort"], "high")

    def test_model_id_from_env(self):
        with mock.patch.dict(os.environ, {"BASETEN_MODEL": "meta-models/muse-spark-open"}):
            self.assertEqual(Model(api_key=self.key).model_id, "meta-models/muse-spark-open")

    def test_errors_never_carry_the_key(self):
        def opener(req, timeout):
            raise urllib.error.URLError("down")

        with self.assertRaises(ModelError) as e:
            Model(api_key=self.key, opener=opener).chat([])
        self.assertNotIn(self.key, str(e.exception))
        with self.assertRaises(ModelError):
            Model(api_key="").chat([])

    def test_http_error_keeps_only_the_status(self):
        body = ('{"error": "bad request", "echo": "Bearer ' + self.key + ' customer said: my address is 12 Nile St"}').encode()

        def opener(req, timeout):
            raise urllib.error.HTTPError(ENDPOINT, 401, "Unauthorized " + self.key, {}, io.BytesIO(body))

        with self.assertRaises(ModelError) as e:
            Model(api_key=self.key, opener=opener).chat([])
        text = str(e.exception)
        self.assertEqual(text, "Baseten answered 401 Unauthorized")
        self.assertNotIn(self.key, text)
        self.assertNotIn("Nile", text)


class SeenAndChecksTest(unittest.TestCase):
    def setUp(self):
        self.db = Db(":memory:")
        self.addCleanup(self.db.close)
        self.chats = Chats(self.db)

    def test_pending_until_the_user_step_is_saved(self):
        self.assertTrue(self.chats.first_seen("wamid.1", {"message_id": "wamid.1", "text": "hi"}))
        self.assertFalse(self.chats.first_seen("wamid.1", {"message_id": "wamid.1"}))
        self.assertEqual(Chats(self.db).pending(), [{"message_id": "wamid.1", "text": "hi"}])
        cid = self.chats.open("whatsapp", "2011")
        self.chats.add_step(cid, "user", content="hi", message_id="wamid.1")
        self.assertEqual(self.chats.pending(), [])

    def test_pending_comes_back_at_most_three_times(self):
        self.chats.first_seen("wamid.2", {"message_id": "wamid.2"})
        got = [len(self.chats.pending()) for _ in range(5)]
        self.assertEqual(got, [1, 1, 1, 0, 0])

    def test_forget_lets_a_retry_in(self):
        self.chats.first_seen("wamid.3", {})
        self.chats.forget("wamid.3")
        self.assertTrue(self.chats.first_seen("wamid.3", {}))

    def test_old_seen_table_gets_the_new_columns(self):
        db = Db(":memory:")
        self.addCleanup(db.close)
        db.script("CREATE TABLE seen_messages (message_id TEXT PRIMARY KEY, at REAL NOT NULL);"
                  "INSERT INTO seen_messages VALUES ('old', 1.0);")
        chats = Chats(db)
        self.assertEqual(chats.pending(), [])
        self.assertTrue(chats.first_seen("new", {"message_id": "new"}))
        self.assertEqual(chats.pending(), [{"message_id": "new"}])

    def test_failed_checks_reset_and_branch_follows(self):
        cid = self.chats.open("whatsapp", "2012")
        self.chats.add_step(cid, "user", content="hi")
        self.chats.add_failed_check(cid, "x")
        self.chats.add_failed_check(cid, "y")
        self.chats.reset_failed_checks(cid)
        self.chats.reset_failed_checks(cid)  # no second event when already 0
        self.chats.add_failed_check(cid, "z")
        self.assertEqual(self.chats.failed_checks(cid), 1)
        events = [s["content"] for s in self.chats.steps(cid) if s["role"] == "event"]
        self.assertEqual(sum('"checks_reset"' in e for e in events), 1)
        self.assertEqual(self.chats.failed_checks(self.chats.branch(cid, len(self.chats.steps(cid)))), 1)


    def test_model_errors_reset_and_branch_follows(self):
        cid = self.chats.open("whatsapp", "2013")
        self.chats.add_step(cid, "user", content="hi")
        self.assertEqual(self.chats.add_model_error(cid, "Baseten call failed: URLError"), 1)
        self.assertEqual(self.chats.add_model_error(cid, "Baseten call failed: HTTP 429"), 2)
        at_two = len(self.chats.steps(cid))
        self.chats.reset_model_errors(cid)
        self.chats.reset_model_errors(cid)  # no second event when already 0
        self.chats.add_model_error(cid, "Baseten call failed: URLError")
        self.assertEqual(self.chats.model_errors(cid), 1)
        events = [s["content"] for s in self.chats.steps(cid) if s["role"] == "event"]
        self.assertEqual(sum('"model_errors_reset"' in e for e in events), 1)
        self.assertEqual(self.chats.model_errors(self.chats.branch(cid, len(self.chats.steps(cid)))), 1)
        self.assertEqual(self.chats.model_errors(self.chats.branch(cid, at_two)), 2)
        self.assertEqual(self.chats.failed_checks(cid), 0)

    def test_old_chats_table_gets_the_model_error_count(self):
        db = Db(":memory:")
        self.addCleanup(db.close)
        db.script("CREATE TABLE chats (id TEXT PRIMARY KEY, channel TEXT NOT NULL, customer_id TEXT NOT NULL, "
                  "stage TEXT NOT NULL DEFAULT 'asking', failed_checks INTEGER NOT NULL DEFAULT 0, parent_id TEXT, "
                  "branched_at INTEGER, created_at REAL NOT NULL, updated_at REAL NOT NULL);"
                  "INSERT INTO chats VALUES ('old', 'whatsapp', '2014', 'asking', 0, NULL, NULL, 1.0, 1.0);")
        chats = Chats(db)
        self.assertEqual(chats.model_errors("old"), 0)
        cid = chats.open("whatsapp", "2015")
        self.assertEqual((chats.add_model_error(cid, "x"), chats.stage(cid), chats.failed_checks(cid)), (1, "asking", 0))
        self.assertEqual(chats.get(chats.branch(cid, 1))["model_errors"], 1)


class StoreTest(unittest.TestCase):
    def setUp(self):
        clean_env(self)

    def test_demo_catalogue(self):
        items = store.catalogue()
        self.assertEqual(len(items), 10)
        adult = [i for i in items if i["title"].startswith("Nilestep Socks")]
        self.assertEqual({v["price"] for i in adult for v in i["variants"]}, {"949"})
        black_l = [v for i in items if i["title"] == "Nilestep Socks, Black" for v in i["variants"] if v["size"].startswith("L")]
        self.assertEqual(black_l[0]["stock"], "out")

    def test_only_fields_in_the_data(self):
        for item in store.catalogue():
            self.assertLessEqual(set(item), {"title", "type", "description", "variants"})
            for v in item["variants"]:
                self.assertLessEqual(set(v), {"size", "price", "sku", "stock"})
                self.assertIn(v.get("stock"), ("in stock", "few left", "out"))
                self.assertNotIn("currency", v)

    def test_search_and_checks(self):
        hits = store.run({"query": "black socks"}, None)["items"]
        self.assertTrue(hits and all("Black" in i["title"] for i in hits))
        self.assertEqual(len(store.run({"query": "zzz"}, None)["items"]), 10)
        self.assertFalse(store.check({"query": "x", "price": 1}, None)[0])
        self.assertFalse(store.check({"query": "x" * 101}, None)[0])

    def test_stock_is_a_level_never_a_count(self):
        self.assertEqual([store.stock_level(q) for q in (-1, 0, 1, 4, 5, 31)],
                         ["out", "out", "few left", "few left", "in stock", "in stock"])
        self.assertEqual(store.FEW_LEFT, 5)
        out = store.run({"query": ""}, None)
        for item in out["items"]:
            for v in item["variants"]:
                self.assertNotIn("available", v)
                self.assertFalse(any(isinstance(x, int) for x in v.values()), v)

    def test_size_strings(self):
        self.assertIn("M (EU 42-45)", store.size_strings())

    def test_price_points(self):
        pts = store.price_points()
        self.assertTrue({949, 649, 749, 60, 85} <= pts)
        self.assertNotIn(349, pts)


class EnvTest(unittest.TestCase):
    def test_load(self):
        with tempfile.TemporaryDirectory() as d:
            p = Path(d) / ".env"
            p.write_text("# comment\nA_TEST_ONE=one\nexport A_TEST_TWO='two'\nA_TEST_KEEP=new\n", encoding="utf-8")
            with mock.patch.dict(os.environ, {"A_TEST_KEEP": "old"}):
                self.assertEqual(sorted(env.load(p)), ["A_TEST_ONE", "A_TEST_TWO"])
                self.assertEqual((os.environ["A_TEST_TWO"], os.environ["A_TEST_KEEP"]), ("two", "old"))
            os.environ.pop("A_TEST_ONE", None)
            os.environ.pop("A_TEST_TWO", None)


if __name__ == "__main__":
    unittest.main()
