import base64
import dataclasses
import hashlib
import json
import types
import unittest
from unittest import mock

from channels.base import Image, Message
from frontdesk import loop, stages
from kit import tools
from kit.chats import Chats
from kit.db import Db
from kit.limits import Limits
from kit.model import ModelError
from kit.tools import voice_note
from kit.tools.handoff import SUMMARY_LABEL, TAKING_OVER
from learning.lessons import Lessons
from learning.results import Results
from tests.fake_model import FakeModel, Outbox, call, clean_env, say, tool_names

CUSTOMER = "201111111111"
OPERATOR = "201000000001"
JPEG = b"\xff\xd8\xff\xe0" + bytes(range(256)) * 4


MODEL_DOWN_AR = loop.MODEL_DOWN["arabic"]


def down(messages, tools_sent):
    raise ModelError("Baseten call failed: URLError")


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
        ctx = tools.Context("c", stages.STAGES["my_order"], None, None, None, print, stages=stages.STAGES)
        self.assertFalse(tools.run("store", {}, ctx)["ok"])
        self.assertFalse(tools.run("made_up", {}, ctx)["ok"])
        self.assertFalse(tools.run("switch_stage", {"to": "with_person"}, ctx)["ok"])
        self.assertFalse(tools.run("switch_stage", {"to": "my_order"}, ctx)["ok"])
        self.assertTrue(tools.run("switch_stage", {"to": "asking"}, ctx)["ok"])


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
        self.assertEqual(self.chats.failed_checks(r.chat_id), 0)  # a passing reply starts the count again

    def test_failed_checks_reset_after_a_passing_reply(self):
        clean_env(self, OPERATOR_WHATSAPP=OPERATOR)
        for _ in range(3):
            r = self.turn(FakeModel(say("It is 1 EGP."), say("Ask me anything.")), "hello there, a question")
            self.assertEqual(r.outcome, "replied")
        r = self.turn(FakeModel(say("It is 1 EGP."), say("It is 1 EGP."), say("It is 1 EGP."), say("ملخص")),
                      "hello there, a question")
        self.assertEqual(r.outcome, "handed_off")
        self.assertEqual(self.outcomes(r.chat_id)[-4:], ["check_failed"] * 3 + ["handed_off"])

    def test_voice_transcript_adds_no_amount(self):
        model = FakeModel(call("voice_note"), say('{"price": 5}'), say("The black hoodie is 5 EGP."),
                          say("I will check the price with the team."))
        r = self.turn(model, "", audio=b"RIFF fake", audio_mime="audio/wav")
        self.assertEqual(self.out.sent, [(CUSTOMER, "I will check the price with the team.")])
        self.assertEqual(self.outcomes(r.chat_id), ["check_failed", "replied"])

    def test_three_calls_per_step_and_one_transcription(self):
        many = {"role": "assistant", "content": None, "tool_calls": [
            {"id": f"c{i}", "type": "function", "function": {"name": "voice_note", "arguments": "{}"}} for i in range(6)]}
        model = FakeModel(many, say("عايز شراب"), say("أيوه عندنا."))
        with mock.patch.object(voice_note, "to_wav", return_value=b"RIFF0000WAVE") as conv:
            r = self.turn(model, "", audio=b"OggS fake", audio_mime="audio/ogg")
        self.assertEqual((r.outcome, len(model.calls), conv.call_count), ("replied", 3, 1))
        results = [json.loads(s["content"]) for s in self.chats.steps(r.chat_id) if s["role"] == "tool"]
        self.assertEqual(len(results), 6)
        self.assertEqual([x.get("ok") for x in results], [True, True, True, False, False, False])
        self.assertIn("At most 3", results[3]["feedback"])
        self.assertEqual(loop.MAX_CALLS_PER_STEP, 3)

    def test_tool_arguments_as_object_or_junk(self):
        obj = {"role": "assistant", "content": None, "tool_calls": [
            {"id": "c1", "type": "function", "function": {"name": "store", "arguments": {"query": "black socks"}}},
            {"id": "c2", "type": "function", "function": {"name": "store", "arguments": 123}}]}
        r = self.turn(FakeModel(obj, say("Yes, black socks are 949 EGP.")), "black socks?")
        self.assertEqual(r.outcome, "replied")
        results = [json.loads(s["content"]) for s in self.chats.steps(r.chat_id) if s["role"] == "tool"]
        self.assertTrue(results[0]["ok"])
        self.assertEqual(results[1], {"ok": False, "feedback": "Tool arguments must be a JSON object."})

    def test_only_store_results_add_amounts(self):
        self.assertEqual(tools.AMOUNT_TOOLS, ("store",))
        fake = types.SimpleNamespace(
            NAME="fake", DEFINITION={"type": "function", "function": {"name": "fake", "parameters": {}}},
            check=lambda args, ctx: (True, ""), run=lambda args, ctx: {"ok": True, "price": 5})
        stage = stages.STAGES["asking"]
        wider = dataclasses.replace(stage, tools=stage.tools + ("fake",))
        with mock.patch.dict(tools.TOOLS, {"fake": fake}), mock.patch.dict(stages.STAGES, {"asking": wider}):
            r = self.turn(FakeModel(call("fake"), say("It costs 5 EGP."), say("I will check with the team.")),
                          "how much?")
        self.assertEqual((r.outcome, self.out.sent), ("replied", [(CUSTOMER, "I will check with the team.")]))
        self.assertEqual(self.outcomes(r.chat_id), ["check_failed", "replied"])

    def test_one_model_error_keeps_the_stage_and_asks_again(self):
        r = self.turn(FakeModel(down), "عندكم شراب؟")
        self.assertEqual((r.outcome, r.reply, self.chats.stage(r.chat_id)), ("model_error", MODEL_DOWN_AR, "asking"))
        self.assertEqual(self.out.sent, [(CUSTOMER, MODEL_DOWN_AR)])
        self.assertEqual(self.chats.model_errors(r.chat_id), 1)
        row = self.results.for_chat(r.chat_id)[-1]
        self.assertEqual((row["outcome"], row["detail"], row["stage"]), ("model_error", {"in_a_row": 1}, "asking"))
        self.assertEqual(self.chats.steps(r.chat_id)[-1]["content"], MODEL_DOWN_AR)

    def test_two_model_errors_in_a_row_hand_off(self):
        clean_env(self, OPERATOR_WHATSAPP=OPERATOR)
        self.assertEqual(self.turn(FakeModel(down), "عندكم شراب؟").outcome, "model_error")
        r = self.turn(FakeModel(down, down), "عندكم شراب؟")
        self.assertEqual((r.outcome, self.chats.stage(r.chat_id)), ("handed_off", "with_person"))
        self.assertEqual([t for to, t in self.out.sent if to == CUSTOMER], [MODEL_DOWN_AR, TAKING_OVER["arabic"]])
        row = self.results.for_chat(r.chat_id)[-1]
        self.assertEqual((row["outcome"], row["detail"]["by"], row["detail"]["in_a_row"]),
                         ("handed_off", "model_error", 2))

    def test_a_passing_reply_starts_the_model_error_count_again(self):
        self.assertEqual(self.turn(FakeModel(down), "hello there, a question").outcome, "model_error")
        r = self.turn(FakeModel(say("Ask me anything.")), "hello there, a question")
        self.assertEqual((r.outcome, self.chats.model_errors(r.chat_id)), ("replied", 0))
        r = self.turn(FakeModel(down), "hello there, a question")
        self.assertEqual((r.outcome, self.chats.stage(r.chat_id)), ("model_error", "asking"))
        self.assertEqual(self.results.for_chat(r.chat_id)[-1]["detail"], {"in_a_row": 1})
        self.assertEqual(self.out.sent[-1], (CUSTOMER, loop.MODEL_DOWN["english"]))

    def test_voice_note_that_failed_to_download(self):
        model = FakeModel()
        r = self.turn(model, "", audio_failed=True)
        self.assertEqual((r.outcome, self.out.sent), ("replied", [(CUSTOMER, loop.VOICE_NOT_HEARD["arabic"])]))
        self.assertEqual(model.calls, [])
        self.assertNotIn("(empty message)", json.dumps(self.chats.steps(r.chat_id), ensure_ascii=False))

    def test_operator_summary_is_checked_and_labelled(self):
        clean_env(self, OPERATOR_WHATSAPP=OPERATOR)
        model = FakeModel(call("handoff", reason="customer wants a person"),
                          say("العميل عايز يكلم حد " + chr(0x1F621) + " **فورا**"))
        self.turn(model, "عايز اكلم حد")
        note = [t for to, t in self.out.sent if to == OPERATOR][0]
        self.assertIn(SUMMARY_LABEL, note)
        self.assertIn("السبب: customer wants a person", note)
        self.assertNotIn(chr(0x1F621), note)
        self.assertNotIn("**", note)

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
        self.assertEqual(loop.MAX_TOOL_STEPS, 4)
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


class SizePriceWarning(unittest.TestCase):
    def setUp(self):
        clean_env(self)

    def test_a_price_that_is_also_a_size_number_is_logged_at_start(self):
        with mock.patch.object(loop.store, "price_points", return_value={45, 949}), \
                mock.patch.object(loop.store, "size_strings", return_value=["M (EU 42-45)"]), \
                self.assertLogs("frontdesk.loop", "WARNING") as logs:
            self.assertEqual(loop.warn_size_prices(), [45])
        self.assertIn("also size numbers: 45.", logs.output[0])

    def test_the_demo_store_has_none(self):
        with self.assertNoLogs("frontdesk.loop", "WARNING"):
            self.assertEqual(loop.warn_size_prices(), [])

    def test_store_errors_do_not_stop_the_start(self):
        with mock.patch.object(loop.store, "price_points", side_effect=OSError("down")), \
                self.assertLogs("frontdesk.loop", "ERROR"):
            self.assertEqual(loop.warn_size_prices(), [])


class LimitTurns(unittest.TestCase):
    def setUp(self):
        clean_env(self)
        self.db = Db(":memory:")
        self.addCleanup(self.db.close)
        self.chats, self.lessons, self.results = Chats(self.db), Lessons(self.db), Results(self.db)
        self.out = Outbox()
        self.now = [1000.0]

    def limits(self, **kw):
        return Limits(self.db, clock=lambda: self.now[0], **kw)

    def turn(self, model, text, limits, customer=CUSTOMER):
        msg = Message(channel="whatsapp", customer_id=customer, text=text)
        return loop.turn(msg, chats=self.chats, model=model, lessons=self.lessons, results=self.results,
                         send=self.out, limits=limits)

    def test_per_customer_limit_and_one_line_per_window(self):
        lim = self.limits(per_customer=2)
        self.assertEqual(self.turn(FakeModel(call("store", query="socks"), say("Yes, 949 EGP.")), "socks?", lim).outcome,
                         "replied")
        later = [self.turn(FakeModel(), "socks?", lim) for _ in range(2)]
        self.assertEqual([r.outcome for r in later], ["limited", "limited"])
        self.assertEqual([t for _, t in self.out.sent[1:]], [loop.LIMIT_LINE["english"]])
        self.now[0] += 601
        self.assertEqual(self.turn(FakeModel(say("Yes, 949 EGP.")), "socks?", lim).outcome, "replied")

    def test_limit_reached_mid_turn(self):
        lim = self.limits(per_customer=1)
        r = self.turn(FakeModel(call("store", query="socks"), say("never reached")), "عندكم شراب؟", lim)
        self.assertEqual((r.outcome, self.out.sent), ("limited", [(CUSTOMER, loop.LIMIT_LINE["arabic"])]))
        self.assertEqual(self.results.for_chat(r.chat_id)[-1]["detail"], {"limit": "customer", "told": True})

    def test_daily_limit_covers_everyone(self):
        lim = self.limits(daily=1)
        self.turn(FakeModel(say("Yes, 949 EGP.")), "socks?", lim)
        r = self.turn(FakeModel(), "3ndko sharabat?", lim, customer="201222222222")
        self.assertEqual((r.outcome, self.out.sent[-1][1]), ("limited", loop.LIMIT_LINE["franco"]))


if __name__ == "__main__":
    unittest.main()
