"""One front desk turn: a customer message in, at most one checked reply out.

1. Load the chat and its stage. With a person, the agent saves the message and waits.
2. Pick the lessons that fit (at most 8) and call the model with the instructions,
   the lessons and only this stage's tools. Tools and stage are sent fresh every step.
3. At most MAX_TOOL_STEPS tool steps, then the model must answer in words. At most
   MAX_CALLS_PER_STEP tool calls run in one step; the voice note is heard once per turn.
4. The send check runs on the reply. A failed reply is never sent: the model retries
   with the check's feedback. After 3 failed checks in a row in a chat, handoff runs
   (three refusals, then a person, as in Meta's legal-agent-workshop). A reply that
   passes starts the count again.
5. When the model is down, handoff runs. Over a model call limit, the customer gets one
   fixed line, once per window.
6. Every outcome goes to the result log.

Photos go to the model as image parts in that turn's user message, in every stage
except with_person. SQLite keeps only their hash and type.
"""
import json
import logging
from dataclasses import dataclass, field
from pathlib import Path

from frontdesk import stages
from kit import limits as limits_mod
from kit import media, send_check, tools
from kit.model import ModelError
from kit.tools import store

log = logging.getLogger("frontdesk.loop")

INSTRUCTIONS = (Path(__file__).parent / "instructions.md").read_text(encoding="utf-8").strip()
MAX_TOOL_STEPS = 4
MAX_CALLS_PER_STEP = 3
MAX_FAILED_CHECKS = 3

PHOTO_NOT_OPENED = {
    "arabic": "مش قادر أفتح الصورة دي. ابعتها تاني صورة عادية وحجمها أقل من 5 ميجا.",
    "franco": "msh 2ader afta7 el sora di. momken teb3atha tany sora 3adeya w a2al mn 5 mega?",
    "english": "I can't open that photo. Please send it again as a normal photo under 5 MB.",
}
VOICE_NOT_HEARD = {
    "arabic": "مش قادر أسمع الفويس ده. ابعته تاني أو اكتبلي رسالتك.",
    "franco": "msh 2ader asma3 el voice da. momken teb3ato tany aw tektebli resaltak?",
    "english": "I can't play that voice note. Please send it again or type your message.",
}
LIMIT_LINE = {
    "arabic": "وصلنا رسايل كتير في وقت قصير، فمش هقدر أرد دلوقتي. ابعتلنا تاني كمان شوية.",
    "franco": "wasalna rasayel kteer f wa2t 2osayar, fa msh ha2dar arod delwa2ty. eb3atlna tany kman shwaya.",
    "english": "We got a lot of messages in a short time, so I can't answer right now. Please write again in a little while.",
}
VOICE_NOTE = "(The customer sent a voice note. Call voice_note to hear it.)"
PHOTO_ONLY = "(The customer sent a photo.)"
RETRY = "(Store check, not from the customer.) Your last reply was not sent. {feedback} Write the reply again."
TOO_MANY_CALLS = {"ok": False, "feedback": f"At most {MAX_CALLS_PER_STEP} tool calls run in one step. This one was not run."}


@dataclass
class TurnResult:
    chat_id: str
    outcome: str          # replied, handed_off, limited, or waiting (a person has the chat)
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


def warn_size_prices():
    """At start: log a warning for each price or fee that is also a catalogue size number.

    After is, for or ب the send check reads a size number as a size, so where prices sit
    among the size numbers, a wrong price there can pass. Returns the numbers found.
    """
    try:
        found = send_check.size_number_amounts(store.price_points(), store.size_strings())
    except Exception:
        log.exception("store data unavailable; prices were not checked against size numbers")
        return []
    if found:
        log.warning("prices or fees that are also size numbers: %s. After is, for or ب the send check "
                    "reads these as sizes, so a wrong price there can pass.", ", ".join(map(str, found)))
    return found


def turn(msg, *, chats, model, lessons, results, send, limits=None, over=None):
    """Handle one customer message. send(to, text) sends on the customer's channel.

    limits: a kit.limits.Limits; every model call in the turn is counted against it.
    over: the limit the webhook already found reached; the turn then makes no model call.
    """
    chat_id = chats.open(msg.channel, msg.customer_id)
    stage = stages.get(chats.stage(chat_id))
    kept = [media.fingerprint("image", i.data, i.mime) for i in msg.images]
    if msg.audio:
        kept.append(media.fingerprint("audio", msg.audio, msg.audio_mime))
    history = chats.messages(chat_id)
    chats.add_step(chat_id, "user", content=msg.text or None, media=kept or None,
                   message_id=msg.message_id or None)
    if not stage.agent_replies:
        return TurnResult(chat_id, "waiting")

    customer_text = msg.text or last_customer_text(chats, chat_id)
    who = limits_mod.key(msg.channel, msg.customer_id)
    if limits is not None:
        over = over or limits.over(who)
        model = limits.meter(model, who)
    if over:
        return _limited(chat_id, stage, msg, customer_text, over, chats=chats, results=results, send=send,
                        limits=limits)

    ctx = tools.Context(chat_id=chat_id, stage=stage, chats=chats, model=model, message=msg,
                        send=send, stages=stages.STAGES)
    t = _Turn(ctx=ctx, customer_text=customer_text, lessons=lessons)
    vocab, sizes = frozenset(), frozenset()
    try:
        t.amounts = set(store.price_points())
        vocab = store.vocabulary()
        sizes = store.size_strings()
    except Exception:
        log.exception("store data unavailable; no price can pass the check this turn")

    script = send_check.detect_script(t.customer_text) or "arabic"
    fixed = []
    if msg.rejected_images:
        fixed.append(("photo_not_opened", PHOTO_NOT_OPENED[script]))
    if msg.audio_failed:
        fixed.append(("voice_not_heard", VOICE_NOT_HEARD[script]))
    n = None
    for _, line in fixed:
        send(msg.customer_id, line)
        n = chats.add_step(chat_id, "assistant", content=line)
    if fixed and not (msg.text or msg.audio or msg.images):
        results.record(chat_id, "replied", step=n, stage=stage.name, detail={"fixed_reply": fixed[-1][0]})
        return TurnResult(chat_id, "replied", fixed[-1][1])

    convo = history + [user_message(msg, stage)]
    try:
        return _answer(t, convo, msg, vocab, sizes, results=results, send=send)
    except limits_mod.OverLimit as e:
        return _limited(chat_id, ctx.stage, msg, t.customer_text, e.which, chats=chats, results=results, send=send,
                        limits=limits)
    except ModelError as e:
        log.warning("model error, handing the chat to a person: %s", e)
        return _hand_off(t, results, "the model is not answering", {"by": "model_error"})


def _answer(t, convo, msg, vocab, sizes, *, results, send):
    ctx = t.ctx
    chat_id = ctx.chat_id
    while True:
        reply = _draft(t, convo)
        if not ctx.stage.agent_replies:
            results.record(chat_id, "handed_off", stage=ctx.stage.name, lessons=t.lesson_ids,
                           detail={"by": "model", "tool_steps": t.tool_steps})
            return TurnResult(chat_id, "handed_off", ctx.sent[-1] if ctx.sent else None)
        passed, feedback = send_check.check(reply, t.customer_text, t.amounts, vocab, sizes)
        if passed:
            send(msg.customer_id, reply)
            n = ctx.chats.add_step(chat_id, "assistant", content=reply)
            ctx.chats.reset_failed_checks(chat_id)
            results.record(chat_id, "replied", step=n, stage=ctx.stage.name, lessons=t.lesson_ids,
                           detail={"tool_steps": t.tool_steps})
            return TurnResult(chat_id, "replied", reply)
        failed = ctx.chats.add_failed_check(chat_id, feedback)
        results.record(chat_id, "check_failed", stage=ctx.stage.name, lessons=t.lesson_ids,
                       detail={"reply": reply, "feedback": feedback})
        if failed >= MAX_FAILED_CHECKS:
            return _hand_off(t, results, f"{failed} replies failed the send check", {"by": "send_check"})
        convo.append({"role": "assistant", "content": reply})
        convo.append({"role": "user", "content": RETRY.format(feedback=feedback)})


def _hand_off(t, results, reason, detail):
    ctx = t.ctx
    stage = ctx.stage
    start = len(ctx.sent)
    out = tools.run("handoff", {"reason": reason}, ctx)
    _apply_stage(t, out)
    _save_sent(t, start)
    results.record(ctx.chat_id, "handed_off", stage=stage.name, lessons=t.lesson_ids,
                   detail={**detail, "ok": bool(out.get("ok"))})
    return TurnResult(ctx.chat_id, "handed_off", ctx.sent[-1] if ctx.sent else None)


def _limited(chat_id, stage, msg, customer_text, which, *, chats, results, send, limits):
    """Over a model call limit: one fixed line, once per window, and no model call."""
    line = None
    if limits is None or limits.notice_due(limits_mod.key(msg.channel, msg.customer_id), which):
        line = LIMIT_LINE[send_check.detect_script(customer_text) or "arabic"]
        send(msg.customer_id, line)
        chats.add_step(chat_id, "assistant", content=line)
    results.record(chat_id, "limited", stage=stage.name, detail={"limit": which, "told": line is not None})
    return TurnResult(chat_id, "limited", line)


def _arguments(raw):
    """Tool arguments as a dict, or None. Some servers send a JSON string, others an object."""
    if isinstance(raw, dict):
        return raw
    if raw is None or raw == "":
        return {}
    try:
        return json.loads(raw)
    except (TypeError, ValueError):
        return None


def _draft(t, convo):
    """Run model steps until it answers in words. Returns the draft reply, or None after a handoff."""
    ctx = t.ctx
    for step in range(MAX_TOOL_STEPS + 1):
        last = step == MAX_TOOL_STEPS
        picked = _lessons(t)
        defs = [] if last else tools.definitions(ctx.stage)
        out = ctx.model.chat([system_prompt(ctx.stage, picked)] + convo, tools=defs or None)
        calls = out.get("tool_calls") or []
        calls = [c for c in calls if isinstance(c, dict)] if isinstance(calls, list) else []
        if not calls or last:
            return (out.get("content") or "").strip()
        t.tool_steps += 1
        call_msg = {"role": "assistant", "content": out.get("content"), "tool_calls": calls}
        convo.append(call_msg)
        ctx.chats.add_step(ctx.chat_id, "assistant", content=out.get("content"), tool_calls=calls)
        sent_before = len(ctx.sent)
        for i, call in enumerate(calls):
            fn = call.get("function") if isinstance(call.get("function"), dict) else {}
            name = fn.get("name") if isinstance(fn.get("name"), str) else ""
            if i >= MAX_CALLS_PER_STEP:
                result = dict(TOO_MANY_CALLS)
            else:
                result = tools.run(name, _arguments(fn.get("arguments")), ctx)
            body = json.dumps(result, ensure_ascii=False)
            call_id = call.get("id") if isinstance(call.get("id"), str) else ""
            convo.append({"role": "tool", "tool_call_id": call_id, "content": body})
            ctx.chats.add_step(ctx.chat_id, "tool", content=body, tool_call_id=call_id, name=name)
            if name in tools.AMOUNT_TOOLS:
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
