"""A scripted offline model with the same chat() shape as kit.model.Model.

Each script item is the assistant message to return, or a function
(messages, tools) -> message for a test that wants to look at the request.
"""
import copy
import itertools
import json
import os
from unittest import mock

_ids = itertools.count(1)


def say(text):
    return {"role": "assistant", "content": text}


def call(name, **args):
    return {"role": "assistant", "content": None, "tool_calls": [{
        "id": f"call_{next(_ids)}", "type": "function",
        "function": {"name": name, "arguments": json.dumps(args, ensure_ascii=False)}}]}


class FakeModel:
    def __init__(self, *script):
        self.script = list(script)
        self.calls = []

    def chat(self, messages, tools=None, effort="medium", max_tokens=1500):
        self.calls.append({"messages": copy.deepcopy(messages), "tools": copy.deepcopy(tools), "effort": effort})
        if not self.script:
            raise AssertionError("the fake model ran out of script")
        nxt = self.script.pop(0)
        return nxt(messages, tools) if callable(nxt) else nxt


class Outbox:
    """Stands in for channel.send: keeps every (to, text)."""

    def __init__(self):
        self.sent = []

    def __call__(self, to, text):
        self.sent.append((to, text))
        return {"ok": True}


def tool_names(tools):
    return [t["function"]["name"] for t in tools or []]


def clean_env(test, **extra):
    """Run a test with no Shopify keys and no operator number unless given."""
    env = {k: v for k, v in os.environ.items()
           if k not in ("SHOPIFY_STORE", "SHOPIFY_TOKEN", "OPERATOR_WHATSAPP", "BASETEN_MODEL")}
    env.update(extra)
    p = mock.patch.dict(os.environ, env, clear=True)
    p.start()
    test.addCleanup(p.stop)
    from kit.tools import store
    store._cache.update(at=0.0, items=None)
