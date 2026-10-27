"""switch_stage: the model moves the chat to another stage when the customer moves on.

Offered in every stage where the agent replies. The allowed targets come from
the current stage, so the model can never jump to "with_person" this way; only
handoff does that, because it also tells the team and the customer.
"""
NAME = "switch_stage"


def definition(stage):
    return {"type": "function", "function": {
        "name": NAME,
        "description": (
            "Move this chat to another stage when the customer moves on, for example from asking "
            "about products to wanting to buy, or to asking about an order they already placed. "
            "The tools for the new stage are offered on the next step."),
        "parameters": {
            "type": "object",
            "properties": {
                "to": {"type": "string", "enum": list(stage.moves_to)},
                "reason": {"type": "string", "description": "One short line on why, in English."},
            },
            "required": ["to"],
            "additionalProperties": False,
        },
    }}


def check(args, ctx):
    to = args.get("to")
    if to not in ctx.stage.moves_to:
        allowed = ", ".join(ctx.stage.moves_to) or "none"
        return False, f"Cannot move from {ctx.stage.name} to {to}. Allowed: {allowed}."
    reason = args.get("reason", "")
    if not isinstance(reason, str) or len(reason) > 300:
        return False, "reason must be one short line."
    return True, ""


def run(args, ctx):
    return {"ok": True, "next_stage": args["to"]}
