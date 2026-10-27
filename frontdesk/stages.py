"""The four stages of a front desk chat, and the tools each stage offers.

GRID is the stage-to-tools grid from the approved layout (28 Oct), as is. A tool that is
not built yet has no file in kit/tools, so the registry leaves it out; it appears in its
stage on the day its file lands. Each turn the model is sent only the offered tools.

How a chat changes stage: from tool results, never from keyword rules.
- switch_stage (a small control tool, offered in the three agent stages) moves between
  asking, ordering and my_order when the customer moves on. The model already reads the
  message in three languages and Franco; a keyword table would not.
- handoff moves to with_person, because it also tells the team and the customer.
- Leaving with_person is a person's call (not built today); until then the agent waits.
"""
from dataclasses import dataclass


@dataclass(frozen=True)
class Stage:
    name: str
    label: str
    about: str
    tools: tuple          # the full row from the grid, built or not
    moves_to: tuple       # where switch_stage may take the chat from here
    agent_replies: bool = True
    images: bool = True   # customer photos go to the model in this stage


GRID = {
    "asking": ("store", "voice_note", "handoff", "delivery"),
    "ordering": ("store", "voice_note", "handoff", "delivery", "draft_order", "cod_risk"),
    "my_order": ("voice_note", "handoff", "find_order", "cancel"),
    "with_person": (),
}

STAGES = {
    "asking": Stage("asking", "Asking", "price, size, stock", GRID["asking"], ("ordering", "my_order")),
    "ordering": Stage("ordering", "Ordering", "wants to buy", GRID["ordering"], ("asking", "my_order")),
    "my_order": Stage("my_order", "My order", "where is it, cancel", GRID["my_order"], ("asking", "ordering")),
    "with_person": Stage("with_person", "With a person", "agent waits", GRID["with_person"], (),
                         agent_replies=False, images=False),
}

FIRST = "asking"


def get(name):
    return STAGES.get(name, STAGES[FIRST])


def note(stage):
    """The line the model reads about where the chat is."""
    others = ", ".join(f"{STAGES[s].name} ({STAGES[s].about})" for s in stage.moves_to)
    return (f"This chat is at the stage {stage.name}: {stage.about}. "
            f"If the customer moves on, call switch_stage. From here it can go to: {others}.")
