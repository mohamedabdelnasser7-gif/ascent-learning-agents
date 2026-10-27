"""One front desk turn: a customer message in, at most one checked reply out.

1. Load the chat and its stage. With a person, the agent saves the message and waits.
2. Pick the lessons that fit (at most 8) and call the model with the instructions,
   the lessons and only this stage's tools. Tools and stage are sent fresh every step.
3. At most MAX_TOOL_STEPS tool steps, then the model must answer in words.
4. The send check runs on the reply. A failed reply is never sent: the model retries
   with the check's feedback. After 3 failed checks in a chat, handoff runs
   (three refusals, then a person, as in Meta's legal-agent-workshop).
5. Every outcome goes to the result log.

Photos go to the model as image parts in that turn's user message, in every stage
except with_person. SQLite keeps only their hash and type.
"""
import json
import logging
from dataclasses import dataclass, field
from pathlib import Path

from frontdesk import stages
from kit import media, send_check, tools
from kit.tools import store

log = logging.getLogger("frontdesk.loop")

INSTRUCTIONS = (Path(__file__).parent / "instructions.md").read_text(encoding="utf-8").strip()
MAX_TOOL_STEPS = 4
MAX_FAILED_CHECKS = 3

PHOTO_NOT_OPENED = {
    "arabic": "مش قادر أفتح الصورة دي. ابعتها تاني صورة عادية وحجمها أقل من 5 ميجا.",
    "franco": "msh 2ader afta7 el sora di. momken teb3atha tany sora 3adeya w a2al mn 5 mega?",
    "english": "I can't open that photo. Please send it again as a normal photo under 5 MB.",
}
VOICE_NOTE = "(The customer sent a voice note. Call voice_note to hear it.)"
PHOTO_ONLY = "(The customer sent a photo.)"
RETRY = "(Store check, not from the customer.) Your last reply was not sent. {feedback} Write the reply again."


@dataclass
class TurnResult:
    chat_id: str
    outcome: str          # replied, handed_off, or waiting (a person has the chat)
    reply: str | None = None


@dataclass
class _Turn:
    ctx: tools.Context
    customer_text: str
    lessons: object = None
    amounts: set = field(default_factory=set)
    lesson_ids: set = field(default_factory=set)
    tool_steps: int = 0


def system_prompt(stage, picked):
    parts = [INSTRUCTIONS, stages.note(stage)]
    if picked:
        parts.append("Lessons from past chats, checked by the team:\n" + "\n".join(f"- {p['text']}" for p in picked))
    return {"role": "system", "content": "\n\n".join(parts)}


def user_message(msg, stage):
    text = msg.text or ""
    if msg.audio:
        text = (text + "\n" + VOICE_NOTE).strip()
    images = msg.images if stage.images else []
    if not images:
        return {"role": "user", "content": text or "(empty message)"}
    parts = [{"type": "text", "text": text or PHOTO_ONLY}]
    parts += [media.image_part(i.data, i.mime) for i in images]
    return {"role": "user", "content": parts}


def last_customer_text(chats, chat_id):
    for s in reversed(chats.steps(chat_id)):
        if s["role"] == "user" and (s["content"] or "").strip():
            return s["content"]
    return ""


def turn(msg, *, chats, model, lessons, results, send):
    """Handle one customer message. send(to, text) sends on the customer's channel."""
    chat_id = chats.open(msg.channel, msg.customer_id)
    stage = stages.get(chats.stage(chat_id))
    kept = [media.fingerprint("image", i.data, i.mime) for i in msg.images]
    if msg.audio:
        kept.append(media.fingerprint("audio", msg.audio, msg.audio_mime))
    history = chats.messages(chat_id)
    user_n = chats.add_step(chat_id, "user", content=msg.text or None, media=kept or None)
    if not stage.agent_replies:
        return TurnResult(chat_id, "waiting")

    ctx = tools.Context(chat_id=chat_id, stage=stage, chats=chats, model=model, message=msg,
                        send=send, stages=stages.STAGES)
    t = _Turn(ctx=ctx, customer_text=msg.text or last_customer_text(chats, chat_id), lessons=lessons)
    try:
        t.amounts = set(store.price_points())
        vocab = store.vocabulary()
    except Exception:
        log.exception("store data unavailable; no price can pass the check this turn")
        vocab = frozenset()

    if msg.rejected_images:
        line = PHOTO_NOT_OPENED[send_check.detect_script(t.customer_text) or "arabic"]
        send(msg.customer_id, line)
        n = chats.add_step(chat_id, "assistant", content=line)
        if not (msg.text or msg.audio or msg.images):
            results.record(chat_id, "replied", step=n, stage=stage.name, detail={"fixed_reply": "photo_not_opened"})
            return TurnResult(chat_id, "replied", line)

    convo = history + [user_message(msg, stage)]
    while True:
        reply = _draft(t, convo)
        if not ctx.stage.agent_replies:
            results.record(chat_id, "handed_off", stage=stage.name, lessons=t.lesson_ids,
                           detail={"by": "model", "tool_steps": t.tool_steps})
            return TurnResult(chat_id, "handed_off", ctx.sent[-1] if ctx.sent else None)
        passed, feedback = send_check.check(reply, t.customer_text, t.amounts, vocab)
        if passed:
            send(msg.customer_id, reply)
            n = chats.add_step(chat_id, "assistant", content=reply)
            results.record(chat_id, "replied", step=n, stage=ctx.stage.name, lessons=t.lesson_ids,
                           detail={"tool_steps": t.tool_steps})
            return TurnResult(chat_id, "replied", reply)
        failed = chats.add_failed_check(chat_id, feedback)
        results.record(chat_id, "check_failed", stage=ctx.stage.name, lessons=t.lesson_ids,
                       detail={"reply": reply, "feedback": feedback})
        if failed >= MAX_FAILED_CHECKS:
            out = tools.run("handoff", {"reason": f"{failed} replies failed the send check"}, ctx)
            _apply_stage(t, out)
            _save_sent(t, 0)
            results.record(chat_id, "handed_off", stage=stage.name, lessons=t.lesson_ids,
                           detail={"by": "send_check", "ok": bool(out.get("ok"))})
            return TurnResult(chat_id, "handed_off", ctx.sent[-1] if ctx.sent else None)
        convo.append({"role": "assistant", "content": reply})
        convo.append({"role": "user", "content": RETRY.format(feedback=feedback)})


def _draft(t, convo):
    """Run model steps until it answers in words. Returns the draft reply, or None after a handoff."""
    ctx = t.ctx
    for step in range(MAX_TOOL_STEPS + 1):
        last = step == MAX_TOOL_STEPS
        picked = _lessons(t)
        defs = [] if last else tools.definitions(ctx.stage)
        out = ctx.model.chat([system_prompt(ctx.stage, picked)] + convo, tools=defs or None)
        calls = out.get("tool_calls") or []
        if not calls or last:
            return (out.get("content") or "").strip()
        t.tool_steps += 1
        call_msg = {"role": "assistant", "content": out.get("content"), "tool_calls": calls}
        convo.append(call_msg)
        ctx.chats.add_step(ctx.chat_id, "assistant", content=out.get("content"), tool_calls=calls)
        sent_before = len(ctx.sent)
        for call in calls:
            fn = call.get("function") or {}
            name = fn.get("name", "")
            try:
                args = json.loads(fn.get("arguments") or "{}")
            except ValueError:
                args = None
            result = tools.run(name, args, ctx)
            body = json.dumps(result, ensure_ascii=False)
            convo.append({"role": "tool", "tool_call_id": call.get("id", ""), "content": body})
            ctx.chats.add_step(ctx.chat_id, "tool", content=body, tool_call_id=call.get("id", ""), name=name)
            t.amounts |= send_check.amounts_in(result)
            if name == "voice_note" and result.get("ok"):
                t.customer_text = result.get("text", "")
            _apply_stage(t, result)
        _save_sent(t, sent_before)
        if not ctx.stage.agent_replies:
            return None
    return ""


def _lessons(t):
    """Lessons for the stage the chat is in now; it can change mid-turn."""
    picked = t.lessons.pick(t.ctx.stage.name, t.customer_text) if t.lessons is not None else []
    t.lesson_ids |= {p["id"] for p in picked}
    return picked


def _apply_stage(t, result):
    nxt = result.get("next_stage") if isinstance(result, dict) else None
    if nxt in stages.STAGES:
        t.ctx.chats.set_stage(t.ctx.chat_id, nxt)
        t.ctx.stage = stages.STAGES[nxt]


def _save_sent(t, start):
    for line in t.ctx.sent[start:]:
        t.ctx.chats.add_step(t.ctx.chat_id, "assistant", content=line)
