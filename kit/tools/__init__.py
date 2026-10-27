"""The tool registry. One file per tool, each with its own checks.

Every tool module has:
  NAME
  DEFINITION, or definition(stage), an OpenAI-style function definition
  check(args, ctx) -> (passed, feedback), run before the tool does anything
  run(args, ctx) -> dict, the result the model reads

A result may carry "next_stage". The agent's loop applies it, so stage changes
come from tool results, never from keyword rules.
"""
import logging
from dataclasses import dataclass, field
from typing import Any, Callable

from kit.tools import handoff, store, switch_stage, voice_note

log = logging.getLogger("kit.tools")

TOOLS = {m.NAME: m for m in (store, voice_note, handoff, switch_stage)}

# Control tools are offered in every stage where the agent replies, on top of the stage's own list.
CONTROL = ("switch_stage",)

# The only tools whose results may add amounts to the send check: they read store or delivery data.
# A voice note or any other free text never adds an amount.
AMOUNT_TOOLS = ("store", "delivery", "draft_order", "find_order")


@dataclass
class Context:
    """What a tool may use during one turn."""
    chat_id: str
    stage: Any                      # the chat's Stage (has .name, .tools, .moves_to)
    chats: Any                      # kit.chats.Chats
    model: Any                      # kit.model.Model or the fake model
    message: Any                    # channels.base.Message
    send: Callable[[str, str], Any] # send(to, text) on the customer's channel
    stages: dict = field(default_factory=dict)
    sent: list = field(default_factory=list)   # texts already sent to the customer this turn
    cache: dict = field(default_factory=dict)  # per-turn results a tool keeps (the voice note, heard once)


def offered(stage):
    """Tool names the model may call in this stage: the stage's built tools, then control tools."""
    if not stage.agent_replies:
        return []
    names = [n for n in stage.tools if n in TOOLS]
    return names + [n for n in CONTROL if n in TOOLS and stage.moves_to]


def definitions(stage):
    """The function definitions sent to the model for this stage, fresh every step."""
    out = []
    for name in offered(stage):
        mod = TOOLS[name]
        out.append(mod.definition(stage) if hasattr(mod, "definition") else mod.DEFINITION)
    return out


def run(name, args, ctx):
    """Run one tool call from the model. Never raises; problems come back as feedback."""
    if name not in offered(ctx.stage):
        return {"ok": False, "feedback": f"The tool {name} is not available at this stage."}
    if not isinstance(args, dict):
        return {"ok": False, "feedback": "Tool arguments must be a JSON object."}
    mod = TOOLS[name]
    passed, feedback = mod.check(args, ctx)
    if not passed:
        return {"ok": False, "feedback": feedback}
    try:
        return mod.run(args, ctx)
    except Exception:  # a tool failure must not end the turn
        log.exception("tool %s failed", name)
        return {"ok": False, "feedback": f"The tool {name} failed. Say you will check with the team."}
