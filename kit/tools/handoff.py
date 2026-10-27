"""handoff: a team member takes over the chat.

Marks the chat with_person (the agent then waits), has the model write a short
Egyptian Arabic summary for the operator, sends it to OPERATOR_WHATSAPP, and tells
the customer in their own language and script that a team member is taking over.
"""
import logging

from kit import env
from kit.model import EFFORT_REPLY
from kit.send_check import DASHES, detect_script, style_problems

log = logging.getLogger("kit.tools.handoff")

NAME = "handoff"
DEFINITION = {"type": "function", "function": {
    "name": NAME,
    "description": (
        "Hand the chat to a team member. Use it when the customer is angry, asks for a person, "
        "wants to cancel, or when you are unsure. The customer is told a team member is taking over."),
    "parameters": {
        "type": "object",
        "properties": {"reason": {"type": "string", "description": "One short line on why, in English."}},
        "required": ["reason"],
        "additionalProperties": False,
    },
}}

TAKING_OVER = {
    "arabic": "حد من الفريق هيكمل معاك دلوقتي.",
    "franco": "7ad mn el team hykamel m3ak delwa2ty.",
    "english": "A team member is taking over this chat now.",
}

SUMMARY_PROMPT = (
    "You write for the store team. Summarise this customer chat in Egyptian Arabic, in Arabic letters, "
    "in three short lines at most: what the customer wants, what was already said, and why a person is "
    "needed. No dashes, no greetings.")

# The note to the operator says plainly that the summary is the model's reading of customer messages.
SUMMARY_LABEL = "ملخص لرسايل العميل، كتبه الإيجنت من الشات:"
NO_SUMMARY = "(مفيش ملخص)"


def check(args, ctx):
    reason = args.get("reason")
    if not isinstance(reason, str) or not reason.strip():
        return False, "Give a short reason for the handoff."
    if len(reason) > 300:
        return False, "Keep the reason to one short line."
    return True, ""


def customer_script(ctx):
    """The script of the customer's latest words in this chat, Egyptian Arabic if none."""
    if ctx.message is not None and detect_script(ctx.message.text):
        return detect_script(ctx.message.text)
    for s in reversed(ctx.chats.steps(ctx.chat_id)):
        if s["role"] == "user" and detect_script(s["content"] or ""):
            return detect_script(s["content"])
    return "arabic"


def summary(ctx, reason):
    lines = []
    for m in ctx.chats.messages(ctx.chat_id, last=20):
        if m["role"] in ("user", "assistant") and m.get("content"):
            who = "Customer" if m["role"] == "user" else "Agent"
            lines.append(f"{who}: {m['content']}")
    chat = "\n".join(lines) or "(no text)"
    try:
        out = ctx.model.chat([
            {"role": "system", "content": SUMMARY_PROMPT},
            {"role": "user", "content": f"Reason for handoff: {reason}\n\nChat:\n{chat}"},
        ], effort=EFFORT_REPLY)
        text = DASHES.sub(",", (out.get("content") or "").strip())
    except Exception:
        log.exception("summary failed")
        text = ""
    if text and style_problems(text):
        log.warning("summary failed the style check; sending the reason only")
        text = ""
    if text:
        return text
    reason_line = f"السبب: {DASHES.sub(',', reason)}"
    return NO_SUMMARY if style_problems(reason_line) else reason_line


def run(args, ctx):
    reason = args["reason"].strip()
    ctx.chats.set_stage(ctx.chat_id, "with_person")
    customer = ctx.message.customer_id if ctx.message is not None else ctx.chats.get(ctx.chat_id)["customer_id"]
    operator = env.get("OPERATOR_WHATSAPP")
    told_operator = False
    if operator:
        note = f"محادثة محتاجة حد من الفريق\nالعميل: {customer}\n{SUMMARY_LABEL}\n{summary(ctx, reason)}"
        try:
            ctx.send(operator, note)
            told_operator = True
        except Exception:
            log.exception("could not message the operator")
    else:
        log.warning("OPERATOR_WHATSAPP is not set; the handoff was not sent to a person")
    line = TAKING_OVER[customer_script(ctx)]
    ctx.send(customer, line)
    ctx.sent.append(line)  # the loop saves it after this tool's result, keeping the step order valid
    return {"ok": True, "next_stage": "with_person", "customer_told": True, "operator_told": told_operator}
