"""Baseten client for Muse Spark Open, over urllib (OpenAI-style chat completions).

Model id: BASETEN_MODEL from env, default "meta-models/muse-spark-open-1026", the id that
worked in our 27 Oct tests. The Baseten changelog names it "meta-models/muse-spark-open".
"""
import json
import urllib.error
import urllib.request

from kit import env

ENDPOINT = "https://inference.baseten.co/v1/chat/completions"
DEFAULT_MODEL = "meta-models/muse-spark-open-1026"
CHANGELOG_MODEL = "meta-models/muse-spark-open"

EFFORT_REPLY = "medium"   # live replies to customers
EFFORT_STUDY = "high"     # the night study, later this week


class ModelError(Exception):
    """The model call failed. The message never holds the key."""


class Model:
    def __init__(self, api_key=None, model_id=None, endpoint=ENDPOINT, timeout=90, opener=None):
        self._key = api_key if api_key is not None else env.get("BASETEN_API_KEY")
        self.model_id = model_id or env.get("BASETEN_MODEL", DEFAULT_MODEL)
        self.endpoint = endpoint
        self.timeout = timeout
        self._open = opener or urllib.request.urlopen

    def body(self, messages, tools=None, effort=EFFORT_REPLY, max_tokens=1500):
        body = {
            "model": self.model_id,
            "messages": messages,
            "max_tokens": max_tokens,
            "reasoning_effort": effort,
        }
        if tools:
            body["tools"] = tools
            body["tool_choice"] = "auto"
        return body

    def chat(self, messages, tools=None, effort=EFFORT_REPLY, max_tokens=1500):
        """Send one request. Returns the assistant message dict (content and maybe tool_calls)."""
        if not self._key:
            raise ModelError("BASETEN_API_KEY is not set")
        data = json.dumps(self.body(messages, tools, effort, max_tokens)).encode("utf-8")
        req = urllib.request.Request(self.endpoint, data=data, method="POST", headers={
            "Authorization": "Bearer " + self._key,
            "Content-Type": "application/json",
        })
        try:
            with self._open(req, timeout=self.timeout) as r:
                out = json.loads(r.read().decode("utf-8"))
        except urllib.error.HTTPError as e:
            detail = e.read(500).decode("utf-8", "replace") if e.fp else ""
            raise ModelError(f"Baseten answered {e.code}: {detail}") from None
        except (urllib.error.URLError, TimeoutError, ValueError) as e:
            raise ModelError(f"Baseten call failed: {type(e).__name__}") from None
        try:
            return out["choices"][0]["message"]
        except (KeyError, IndexError, TypeError):
            raise ModelError("Baseten answer had no message") from None
