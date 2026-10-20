"""Thin HTTP clients for Gemini and Hugging Face. Both take the same neutral messages
and return the same LLMReply, so the agent loop doesn't care which one is used.

Neutral messages:
  {"role": "user", "content": str}
  {"role": "assistant", "content": str, "tool_calls": [ToolCall], "raw": provider-native turn}
  {"role": "tool", "tool_call_id": str, "name": str, "content": str}
"""

import json
import time
from typing import Any

import requests
from opentelemetry import trace
from pydantic import BaseModel

from .telemetry import event, llm_retries

RETRY_STATUS = {429, 500, 502, 503, 504}
FATAL_STATUS = {401, 403, 404}  # bad credentials, no access, unknown or retired model
MAX_RATE_LIMIT_WAIT = 90  # seconds; a longer requested wait means the daily quota is gone


class LLMError(RuntimeError):
    pass


class FatalLLMError(LLMError):
    """Every request will fail the same way (bad key, no access, unknown model). Stop the run."""


class QuotaExhausted(FatalLLMError):
    """The API wants us to wait longer than MAX_RATE_LIMIT_WAIT (e.g. daily quota used up)."""


class ToolCall(BaseModel):
    id: str
    name: str
    args: dict


class LLMReply(BaseModel):
    text: str = ""
    tool_calls: list[ToolCall] = []
    usage: dict[str, int] = {}
    raw: Any = None

    def as_message(self) -> dict:
        return {"role": "assistant", "content": self.text, "tool_calls": self.tool_calls, "raw": self.raw}


def _rate_limit_delay(response) -> float | None:
    """Seconds the API asked us to wait: Retry-After header or Gemini's RetryInfo.retryDelay ("37s")."""
    header = getattr(response, "headers", {}).get("Retry-After")
    if header and header.isdigit():
        return float(header)
    try:
        details = response.json()["error"]["details"]
        delay = next(d["retryDelay"] for d in details if "retryDelay" in d)
        return float(delay.rstrip("s"))
    except (ValueError, KeyError, TypeError, StopIteration):
        return None


def _error_message(response) -> str:
    try:
        return str(response.json()["error"]["message"])[:200]
    except (ValueError, KeyError, TypeError):
        return response.text[:200]


def _is_fatal(response) -> bool:
    if response.status_code in FATAL_STATUS:
        return True
    return response.status_code == 400 and "api key" in _error_message(response).lower()


class _HttpClient:
    system = ""

    def __init__(
        self,
        model: str,
        session=None,
        timeout: float = 90,
        retries: int = 2,
        sleep=time.sleep,
        clock=time.monotonic,
        requests_per_minute: int = 0,
        rate_limit_retries: int = 6,
    ):
        self.model = model
        self.session = session or requests.Session()
        self.timeout = timeout
        self.retries = retries
        self.rate_limit_retries = rate_limit_retries
        self.sleep = sleep
        self.clock = clock
        self.min_interval = 60 / requests_per_minute if requests_per_minute else 0
        self._last_request = None

    def _throttle(self) -> None:
        """Space requests out so we stay under the provider's per-minute quota."""
        if self._last_request is not None:
            wait = self._last_request + self.min_interval - self.clock()
            if wait > 0:
                self.sleep(wait)
        self._last_request = self.clock()

    def _post(self, url: str, headers: dict, payload: dict) -> dict:
        """POST with retries. Short rate-limit waits (429) get their own, larger allowance than
        server/network errors, since they're expected on small quotas. Errors never include credentials."""
        failures = rate_limits = attempts = 0
        span = trace.get_current_span()  # the agent's llm.chat span
        while True:
            self._throttle()
            attempts += 1
            span.set_attribute("llm.attempts", attempts)
            try:
                response = self.session.post(url, headers=headers, json=payload, timeout=self.timeout)
            except requests.RequestException as exc:
                failures += 1
                if failures > self.retries:
                    raise LLMError(f"{self.system} request failed: {type(exc).__name__}") from None
                self._retrying(span, attempts, type(exc).__name__, 2**failures)
                continue

            if response.status_code < 400:
                return response.json()
            if _is_fatal(response):
                raise FatalLLMError(f"{self.system} returned HTTP {response.status_code}: {_error_message(response)}")

            if response.status_code == 429:
                wanted = _rate_limit_delay(response)
                if wanted and wanted > MAX_RATE_LIMIT_WAIT:  # waiting won't help today - fail fast
                    raise QuotaExhausted(f"{self.system} quota exhausted; retry in {wanted:.0f}s")
                rate_limits += 1
                if rate_limits > self.rate_limit_retries:
                    raise LLMError(f"{self.system} still rate limited after {self.rate_limit_retries} retries")
                self._retrying(span, attempts, "rate_limited", wanted or 2**rate_limits)
                continue

            failures += 1
            if response.status_code not in RETRY_STATUS or failures > self.retries:
                raise LLMError(f"{self.system} returned HTTP {response.status_code}: {response.text[:200]}")
            self._retrying(span, attempts, f"http_{response.status_code}", 2**failures)

    def _retrying(self, span, attempt: int, reason: str, wait: float) -> None:
        """A failed HTTP attempt that will be retried: visible on the llm.chat span, in the logs and as a
        metric, so a red POST span followed by a green one reads as "succeeded after a retry"."""
        details = {"llm.retry.reason": reason, "llm.retry.attempt": attempt, "llm.retry.wait_s": float(wait)}
        span.add_event("llm.retry", details)
        llm_retries.add(1, {"provider": self.system, "model": self.model, "reason": reason})
        event("llm.retry", f"LLM attempt {attempt} failed ({reason}); retrying in {wait:.0f}s", **details)
        self.sleep(wait)


class GeminiClient(_HttpClient):
    system = "gemini"
    BASE_URL = "https://generativelanguage.googleapis.com/v1beta/models"

    def __init__(self, api_key: str, model: str, **kw):
        super().__init__(model, **kw)
        self.api_key = api_key

    def chat(self, system_prompt: str, messages: list[dict], tools: list[dict]) -> LLMReply:
        payload = {
            "systemInstruction": {"parts": [{"text": system_prompt}]},
            "contents": self._contents(messages),
            "tools": [{"functionDeclarations": tools}],
            "generationConfig": {"temperature": 0},
        }
        url = f"{self.BASE_URL}/{self.model}:generateContent"
        data = self._post(url, {"x-goog-api-key": self.api_key}, payload)

        usage = data.get("usageMetadata", {})
        reply = LLMReply(
            usage={
                "input_tokens": usage.get("promptTokenCount", 0),
                "output_tokens": usage.get("candidatesTokenCount", 0),
            }
        )
        if not data.get("candidates"):  # blocked by safety filters, etc.
            return reply
        content = data["candidates"][0].get("content", {"role": "model", "parts": []})
        parts = content.get("parts", [])
        reply.raw = content  # echoed back unchanged next turn (keeps thought signatures valid)
        reply.text = "".join(part.get("text", "") for part in parts)
        reply.tool_calls = [
            ToolCall(id=f"call_{i}", name=part["functionCall"]["name"], args=part["functionCall"].get("args", {}))
            for i, part in enumerate(p for p in parts if "functionCall" in p)
        ]
        return reply

    @staticmethod
    def _contents(messages: list[dict]) -> list[dict]:
        contents = []
        for msg in messages:
            if msg["role"] == "user":
                contents.append({"role": "user", "parts": [{"text": msg["content"]}]})
            elif msg["role"] == "assistant":
                contents.append(msg.get("raw") or {"role": "model", "parts": [{"text": msg["content"]}]})
            elif msg["role"] == "tool":
                part = {"functionResponse": {"name": msg["name"], "response": {"result": msg["content"]}}}
                previous = contents[-1] if contents else {}
                if previous.get("role") == "user" and "functionResponse" in previous["parts"][0]:
                    previous["parts"].append(part)  # parallel calls -> one turn with several results
                else:
                    contents.append({"role": "user", "parts": [part]})
        return contents


class HuggingFaceClient(_HttpClient):
    """Hugging Face Inference Providers router (OpenAI-compatible chat completions)."""

    system = "huggingface"
    URL = "https://router.huggingface.co/v1/chat/completions"

    def __init__(self, token: str, model: str, **kw):
        super().__init__(model, **kw)
        self.token = token

    def chat(self, system_prompt: str, messages: list[dict], tools: list[dict]) -> LLMReply:
        payload = {
            "model": self.model,
            "messages": [{"role": "system", "content": system_prompt}] + [self._message(m) for m in messages],
            "tools": [{"type": "function", "function": tool} for tool in tools],
            "tool_choice": "auto",
            "temperature": 0,
            "max_tokens": 1024,
        }
        data = self._post(self.URL, {"Authorization": f"Bearer {self.token}"}, payload)

        message = data["choices"][0]["message"]
        usage = data.get("usage", {})
        return LLMReply(
            text=message.get("content") or "",
            tool_calls=[
                ToolCall(
                    id=call.get("id") or f"call_{i}",
                    name=call["function"]["name"],
                    args=self._parse_args(call["function"].get("arguments")),
                )
                for i, call in enumerate(message.get("tool_calls") or [])
            ],
            usage={"input_tokens": usage.get("prompt_tokens", 0), "output_tokens": usage.get("completion_tokens", 0)},
        )

    @staticmethod
    def _parse_args(arguments) -> dict:
        """Some models return arguments as a JSON string, some as an object, some as broken JSON."""
        if isinstance(arguments, dict):
            return arguments
        try:
            parsed = json.loads(arguments or "{}")
        except json.JSONDecodeError:
            return {"_unparseable_arguments": arguments}
        return parsed if isinstance(parsed, dict) else {"_unparseable_arguments": arguments}

    @staticmethod
    def _message(msg: dict) -> dict:
        if msg["role"] == "assistant":
            out = {"role": "assistant", "content": msg["content"] or ""}
            if msg.get("tool_calls"):
                out["tool_calls"] = [
                    {
                        "id": call.id,
                        "type": "function",
                        "function": {"name": call.name, "arguments": json.dumps(call.args)},
                    }
                    for call in msg["tool_calls"]
                ]
            return out
        if msg["role"] == "tool":
            return {"role": "tool", "tool_call_id": msg["tool_call_id"], "name": msg["name"], "content": msg["content"]}
        return {"role": "user", "content": msg["content"]}
