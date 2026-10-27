import base64
import hashlib
import unittest
from unittest import mock

from channels.base import Image, Message
from frontdesk import loop, stages
from kit import tools
from kit.chats import Chats
from kit.db import Db
from kit.tools import voice_note
from kit.tools.handoff import TAKING_OVER
from learning.lessons import Lessons
from learning.results import Results
from tests.fake_model import FakeModel, Outbox, call, clean_env, say, tool_names

CUSTOMER = "201111111111"
OPERATOR = "201000000001"
JPEG = b"\xff\xd8\xff\xe0" + bytes(range(256)) * 4


class StageTools(unittest.TestCase):
    def test_grid_is_the_approved_layout(self):
        self.assertEqual(stages.GRID, {
            "asking": ("store", "voice_note", "handoff", "delivery"),
            "ordering": ("store", "voice_note", "handoff", "delivery", "draft_order", "cod_risk"),
            "my_order": ("voice_note", "handoff", "find_order", "cancel"),
            "with_person": (),
        })

    def test_offered_today(self):
        names = {s: tool_names(tools.definitions(stages.STAGES[s])) for s in stages.STAGES}
        self.assertEqual(names, {
            "asking": ["store", "voice_note", "handoff", "switch_stage"],
            "ordering": ["store", "voice_note", "handoff", "switch_stage"],
            "my_order": ["voice_note", "handoff", "switch_stage"],
            "with_person": [],
        })

    def test_switch_targets(self):
        d = tools.definitions(stages.STAGES["asking"])[-1]["function"]
        self.assertEqual(d["parameters"]["properties"]["to"]["enum"], ["ordering", "my_order"])
        self.assertNotIn("with_person", [t for s in stages.STAGES.values() for t in s.moves_to])

    def test_a_tool_outside_the_stage_is_refused(self):
        ctx = tools.Context("c", stages.STAGES["my_order"], None, None, None, print)
        self.assertFalse(tools.run("store", {}, ctx)["ok"])
        self.assertFalse(tools.run("made_up", {}, ctx)["ok"])
        self.assertFalse(tools.run("switch_stage", {"to": "with_person"}, ctx)["ok"])


class Turns(unittest.TestCase):
    def setUp(self):
        clean_env(self)
        self.db = Db(":memory:")
        self.addCleanup(self.db.close)
        self.chats, self.lessons, self.results = Chats(self.db), Lessons(self.db), Results(self.db)
        self.out = Outbox()

    def turn(self, model, text="", **kw):
        msg = Message(channel="whatsapp", customer_id=CUSTOMER, text=text, **kw)
        return loop.turn(msg, chats=self.chats, model=model, lessons=self.lessons, results=self.results, send=self.out)

    def outcomes(self, chat_id):
        return [r["outcome"] for r in self.results.for_chat(chat_id)]

    def test_full_turn(self):
        self.lessons.add("Say the price plainly, the number then جنيه.", ["asking"])
        model = FakeModel(call("store", query="black socks"),
                          say("عندنا Nilestep Socks الأسود مقاس M بـ 949 جنيه."))
        r = self.turn(model, "عندكم شراب اسود مقاس ٤٣؟ بكام")
        self.assertEqual(r.outcome, "replied")
        self.assertEqual(self.out.sent, [(CUSTOMER, "عندنا Nilestep Socks الأسود مقاس M بـ 949 جنيه.")])
        first, second = model.calls
        self.assertEqual(tool_names(first["tools"]), ["store", "voice_note", "handoff", "switch_stage"])
        system = first["messages"][0]["content"]
        self.assertIn("WhatsApp front desk", system)
        self.assertIn("stage asking", system)
        self.assertIn("Say the price plainly, the number then جنيه.", system)
        self.assertEqual(first["effort"], "medium")
        self.assertIn('"price": "949"', second["messages"][-1]["content"])
        self.assertEqual([s["role"] for s in self.chats.steps(r.chat_id)], ["user", "assistant", "tool", "assistant"])
        self.assertEqual(self.outcomes(r.chat_id), ["replied"])
        self.assertEqual(self.results.for_chat(r.chat_id)[0]["lessons"], [1])

    def test_failed_check_retries_with_feedback(self):
        model = FakeModel(say("349 جنيه للجوز"), say("الشراب بـ 949 جنيه."))
        r = self.turn(model, "الشراب بكام؟")
        self.assertEqual(self.out.sent, [(CUSTOMER, "الشراب بـ 949 جنيه.")])
        retry = model.calls[1]["messages"][-1]["content"]
        self.assertIn("not sent", retry)
        self.assertIn("349", retry)
        self.assertIn("الجوز", retry)
        self.assertEqual(self.outcomes(r.chat_id), ["check_failed", "replied"])
        self.assertEqual(self.chats.failed_checks(r.chat_id), 1)

    def test_three_failed_checks_hand_off(self):
        clean_env(self, OPERATOR_WHATSAPP=OPERATOR)
        model = FakeModel(say("It is 349 EGP."), say("Only 349 EGP."), say("349 EGP, sorry to hear"),
                          say("العميل بيسأل عن سعر الشراب والرد اتمنع 3 مرات."))
        r = self.turn(model, "How much are the socks?")
        self.assertEqual(r.outcome, "handed_off")
        self.assertEqual(self.chats.stage(r.chat_id), "with_person")
        to_operator = [t for to, t in self.out.sent if to == OPERATOR]
        self.assertEqual(len(to_operator), 1)
        self.assertIn(CUSTOMER, to_operator[0])
        self.assertEqual([t for to, t in self.out.sent if to == CUSTOMER], [TAKING_OVER["english"]])
        self.assertEqual(self.outcomes(r.chat_id), ["check_failed"] * 3 + ["handed_off"])
        # with a person, the agent waits: no model call, nothing sent
        sent = len(self.out.sent)
        later = self.turn(FakeModel(), "hello?", images=[Image(JPEG, "image/jpeg")])
        self.assertEqual((later.outcome, len(self.out.sent)), ("waiting", sent))

    def test_model_hands_off_in_franco(self):
        model = FakeModel(call("handoff", reason="customer wants a person"))
        r = self.turn(model, "enta bot wala eh? 3ayez 7ad yklmni")
        self.assertEqual(r.outcome, "handed_off")
        self.assertEqual(self.out.sent, [(CUSTOMER, TAKING_OVER["franco"])])
        roles = [s["role"] for s in self.chats.steps(r.chat_id)]
        self.assertEqual(roles, ["user", "assistant", "event", "tool", "assistant"])

    def test_switch_stage_changes_the_tools_mid_turn(self):
        def answer(messages, tools_sent):
            self.assertEqual(tool_names(tools_sent), ["voice_note", "handoff", "switch_stage"])
            self.assertIn("stage my_order", messages[0]["content"])
            return say("I will check your order with the team and reply here.")

        model = FakeModel(call("switch_stage", to="my_order", reason="asks about an order"), answer)
        r = self.turn(model, "Where is my order?")
        self.assertEqual((r.outcome, self.chats.stage(r.chat_id)), ("replied", "my_order"))

    def test_tool_steps_are_capped(self):
        model = FakeModel(*[call("store", query="socks") for _ in range(loop.MAX_TOOL_STEPS)],
                          say("Yes, we have them at 949 EGP."))
        self.turn(model, "socks?")
        self.assertEqual(len(model.calls), loop.MAX_TOOL_STEPS + 1)
        self.assertIsNone(model.calls[-1]["tools"])

    def test_photo_goes_to_the_model_and_only_its_hash_is_kept(self):
        model = FakeModel(say("aywa 3andena, b 949 geneh."))
        r = self.turn(model, "3ndko de?", images=[Image(JPEG, "image/jpeg")])
        self.assertEqual(r.outcome, "replied")
        user = model.calls[0]["messages"][-1]
        b64 = base64.b64encode(JPEG).decode()
        self.assertEqual(user["content"][0], {"type": "text", "text": "3ndko de?"})
        self.assertEqual(user["content"][1], {"type": "image_url", "image_url": {"url": "data:image/jpeg;base64," + b64}})
        kept = self.chats.steps(r.chat_id)[0]["media"]
        self.assertEqual(kept, [{"kind": "image", "sha256": hashlib.sha256(JPEG).hexdigest(), "mime": "image/jpeg"}])
        dump = "\n".join(self.db.conn.iterdump())
        self.assertNotIn(b64[:200], dump)

    def test_rejected_photo_gets_a_polite_reply(self):
        model = FakeModel()
        r = self.turn(model, "", rejected_images=1)
        self.assertEqual((r.outcome, self.out.sent), ("replied", [(CUSTOMER, loop.PHOTO_NOT_OPENED["arabic"])]))
        self.assertEqual(model.calls, [])

    def test_rejected_photo_with_a_caption_still_gets_an_answer(self):
        model = FakeModel(say("Yes, size 43 is in stock."))
        self.turn(model, "Is this in size 43?", rejected_images=1)
        self.assertEqual([t for _, t in self.out.sent], [loop.PHOTO_NOT_OPENED["english"], "Yes, size 43 is in stock."])

    def test_voice_note(self):
        heard = "عندكم شرابات مقاس ٤٣؟"
        model = FakeModel(call("voice_note"), say(heard), say("أيوه عندنا مقاس 43، بـ 949 جنيه."))
        with mock.patch.object(voice_note, "to_wav", return_value=b"RIFF0000WAVE") as conv:
            r = self.turn(model, "", audio=b"OggS fake", audio_mime="audio/ogg; codecs=opus")
        conv.assert_called_once()
        self.assertEqual(r.outcome, "replied")
        parts = model.calls[1]["messages"][0]["content"]
        self.assertEqual((parts[1]["type"], parts[1]["input_audio"]["format"]), ("input_audio", "wav"))
        self.assertEqual(self.out.sent, [(CUSTOMER, "أيوه عندنا مقاس 43، بـ 949 جنيه.")])
        self.assertEqual(self.chats.steps(r.chat_id)[0]["media"][0]["kind"], "audio")


if __name__ == "__main__":
    unittest.main()
