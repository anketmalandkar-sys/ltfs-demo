import logging

import pytest
import requests

from email_agent.llm import (
    FatalLLMError,
    GeminiClient,
    HuggingFaceClient,
    LLMError,
    QuotaExhausted,
    ToolCall,
)

TOOLS = [{"name": "read_email", "description": "d", "parameters": {"type": "object"}}]


class FakeResponse:
    def __init__(self, status, body):
        self.status_code = status
        self._body = body
        self.text = str(body)

    def json(self):
        return self._body


class FakeSession:
    def __init__(self, *responses):
        self.responses = list(responses)
        self.posts = []

    def post(self, url, headers, json, timeout):
        self.posts.append({"url": url, "headers": headers, "json": json, "timeout": timeout})
        response = self.responses.pop(0)
        if isinstance(response, Exception):  # e.g. requests.ReadTimeout
            raise response
        return response


GEMINI_TOOL_CALL = {
    "candidates": [
        {
            "content": {
                "role": "model",
                "parts": [
                    {"functionCall": {"name": "read_email", "args": {"email_id": "e1"}}, "thoughtSignature": "sig=="},
                ],
            }
        }
    ],
    "usageMetadata": {"promptTokenCount": 120, "candidatesTokenCount": 8},
}
GEMINI_TEXT = {"candidates": [{"content": {"role": "model", "parts": [{"text": "Done."}]}}]}


def gemini(*responses):
    session = FakeSession(*responses)
    return GeminiClient("g-key", "gemini-2.5-flash", session=session, sleep=lambda s: None), session


def test_gemini_request_shape_and_tool_call_parsing():
    client, session = gemini(FakeResponse(200, GEMINI_TOOL_CALL))
    reply = client.chat("SYSTEM", [{"role": "user", "content": "Triage e1"}], TOOLS)
    post = session.posts[0]
    assert post["url"].endswith("/models/gemini-2.5-flash:generateContent")
    assert "key=" not in post["url"] and post["headers"]["x-goog-api-key"] == "g-key"
    assert post["json"]["systemInstruction"]["parts"][0]["text"] == "SYSTEM"
    assert post["json"]["tools"][0]["functionDeclarations"] == TOOLS
    assert post["json"]["generationConfig"]["temperature"] == 0
    assert reply.tool_calls == [ToolCall(id="call_0", name="read_email", args={"email_id": "e1"})]
    assert reply.usage == {"input_tokens": 120, "output_tokens": 8}


def test_gemini_echoes_model_turn_with_thought_signature_and_sends_tool_results():
    client, session = gemini(FakeResponse(200, GEMINI_TOOL_CALL), FakeResponse(200, GEMINI_TEXT))
    messages = [{"role": "user", "content": "Triage e1"}]
    reply = client.chat("S", messages, TOOLS)
    messages.append(reply.as_message())
    messages.append({"role": "tool", "tool_call_id": "call_0", "name": "read_email", "content": "<email>"})
    final = client.chat("S", messages, TOOLS)
    contents = session.posts[1]["json"]["contents"]
    assert contents[1] == GEMINI_TOOL_CALL["candidates"][0]["content"]  # unchanged, signature included
    assert contents[2] == {
        "role": "user",
        "parts": [{"functionResponse": {"name": "read_email", "response": {"result": "<email>"}}}],
    }
    assert final.text == "Done." and final.tool_calls == []


def test_gemini_blocked_response_returns_empty_reply():
    client, _ = gemini(FakeResponse(200, {"promptFeedback": {"blockReason": "SAFETY"}}))
    reply = client.chat("S", [{"role": "user", "content": "x"}], TOOLS)
    assert reply.text == "" and reply.tool_calls == []


def test_retries_on_429_then_succeeds():
    client, session = gemini(FakeResponse(429, {}), FakeResponse(503, {}), FakeResponse(200, GEMINI_TEXT))
    assert client.chat("S", [{"role": "user", "content": "x"}], TOOLS).text == "Done."
    assert len(session.posts) == 3


def test_429_waits_for_the_delay_gemini_asks_for():
    rate_limited = {
        "error": {"code": 429, "details": [{"@type": "type.googleapis.com/google.rpc.RetryInfo", "retryDelay": "37s"}]}
    }
    sleeps = []
    session = FakeSession(FakeResponse(429, rate_limited), FakeResponse(200, GEMINI_TEXT))
    client = GeminiClient("k", "m", session=session, sleep=sleeps.append)
    assert client.chat("S", [{"role": "user", "content": "x"}], TOOLS).text == "Done."
    assert sleeps == [37]


def test_short_rate_limits_get_their_own_larger_retry_allowance():
    rate_limited = {"error": {"code": 429, "details": [{"retryDelay": "10s"}]}}
    sleeps = []
    session = FakeSession(*[FakeResponse(429, rate_limited)] * 5, FakeResponse(200, GEMINI_TEXT))
    client = GeminiClient("k", "m", session=session, sleep=sleeps.append)  # retries=2 for other errors
    assert client.chat("S", [{"role": "user", "content": "x"}], TOOLS).text == "Done."
    assert sleeps == [10] * 5


def test_rate_limit_retries_are_still_bounded():
    rate_limited = {"error": {"code": 429, "details": [{"retryDelay": "10s"}]}}
    session = FakeSession(*[FakeResponse(429, rate_limited)] * 7)
    client = GeminiClient("k", "m", session=session, sleep=lambda s: None, rate_limit_retries=6)
    with pytest.raises(LLMError, match="rate limited") as exc:
        client.chat("S", [{"role": "user", "content": "x"}], TOOLS)
    assert not isinstance(exc.value, FatalLLMError)
    assert len(session.posts) == 7


def test_429_respects_retry_after_header():
    sleeps = []
    response = FakeResponse(429, {})
    response.headers = {"Retry-After": "20"}
    client = GeminiClient("k", "m", session=FakeSession(response, FakeResponse(200, GEMINI_TEXT)), sleep=sleeps.append)
    client.chat("S", [{"role": "user", "content": "x"}], TOOLS)
    assert sleeps == [20]


@pytest.mark.parametrize("retry_delay", ["58055s", "91s"])
def test_429_with_long_delay_means_quota_exhausted_and_fails_fast(retry_delay):
    daily = {
        "error": {
            "code": 429,
            "details": [{"@type": "type.googleapis.com/google.rpc.RetryInfo", "retryDelay": retry_delay}],
        }
    }
    sleeps = []
    session = FakeSession(FakeResponse(429, daily))
    client = GeminiClient("k", "m", session=session, sleep=sleeps.append)
    with pytest.raises(QuotaExhausted):
        client.chat("S", [{"role": "user", "content": "x"}], TOOLS)
    assert sleeps == [] and len(session.posts) == 1
    assert issubclass(QuotaExhausted, LLMError)


def test_requests_per_minute_spaces_out_calls():
    now = [100.0]
    sleeps = []

    def sleep(seconds):
        sleeps.append(seconds)
        now[0] += seconds

    session = FakeSession(*[FakeResponse(200, GEMINI_TEXT)] * 3)
    client = GeminiClient("k", "m", session=session, sleep=sleep, clock=lambda: now[0], requests_per_minute=5)
    for _ in range(3):
        client.chat("S", [{"role": "user", "content": "x"}], TOOLS)
        now[0] += 1  # each call "takes" one second
    assert sleeps == [11, 11]  # 12s between requests at 5/minute


def test_gives_up_after_retries_without_leaking_key():
    client, _ = gemini(*[FakeResponse(500, {"error": "boom"})] * 3)
    with pytest.raises(LLMError) as exc:
        client.chat("S", [{"role": "user", "content": "x"}], TOOLS)
    assert "500" in str(exc.value) and "g-key" not in str(exc.value)


def test_client_error_is_not_retried_and_not_fatal():
    # A plain 400 can be caused by one email's content, so only that email fails.
    client, session = gemini(FakeResponse(400, {"error": "bad request"}))
    with pytest.raises(LLMError) as exc:
        client.chat("S", [{"role": "user", "content": "x"}], TOOLS)
    assert not isinstance(exc.value, FatalLLMError)
    assert len(session.posts) == 1


@pytest.mark.parametrize(
    "status, body",
    [
        (401, {"error": {"message": "Request had invalid authentication credentials."}}),
        (403, {"error": {"message": "Permission denied."}}),
        (404, {"error": {"message": "models/gemini-2.5-flash-lite is no longer available to new users."}}),
        (400, {"error": {"message": "API key not valid. Please pass a valid API key."}}),
    ],
)
def test_errors_that_hit_every_request_are_fatal(status, body):
    sleeps = []
    session = FakeSession(FakeResponse(status, body))
    client = GeminiClient("k", "m", session=session, sleep=sleeps.append)
    with pytest.raises(FatalLLMError, match=str(status)):
        client.chat("S", [{"role": "user", "content": "x"}], TOOLS)
    assert sleeps == [] and len(session.posts) == 1


def test_quota_exhausted_is_a_fatal_error():
    assert issubclass(QuotaExhausted, FatalLLMError) and issubclass(FatalLLMError, LLMError)


HF_TOOL_CALL = {
    "choices": [
        {
            "message": {
                "role": "assistant",
                "content": None,
                "tool_calls": [
                    {
                        "id": "abc",
                        "type": "function",
                        "function": {
                            "name": "submit_triage",
                            "arguments": '{"email_id": "e1", "category": "incident"}',
                        },
                    },
                    {
                        "id": "def",
                        "type": "function",
                        "function": {"name": "read_email", "arguments": {"email_id": "e1"}},
                    },
                    {"id": "ghi", "type": "function", "function": {"name": "read_email", "arguments": "{not json"}},
                ],
            }
        }
    ],
    "usage": {"prompt_tokens": 50, "completion_tokens": 20},
}


def test_huggingface_request_and_argument_parsing():
    session = FakeSession(FakeResponse(200, HF_TOOL_CALL))
    client = HuggingFaceClient("hf-token", "Qwen/Qwen2.5-72B-Instruct", session=session, sleep=lambda s: None)
    reply = client.chat("SYSTEM", [{"role": "user", "content": "Triage e1"}], TOOLS)
    post = session.posts[0]
    assert post["url"] == "https://router.huggingface.co/v1/chat/completions"
    assert post["headers"]["Authorization"] == "Bearer hf-token"
    assert post["json"]["model"] == "Qwen/Qwen2.5-72B-Instruct"
    assert post["json"]["messages"][0] == {"role": "system", "content": "SYSTEM"}
    assert post["json"]["tools"][0] == {"type": "function", "function": TOOLS[0]}
    assert [c.args for c in reply.tool_calls] == [
        {"email_id": "e1", "category": "incident"},
        {"email_id": "e1"},
        {"_unparseable_arguments": "{not json"},
    ]
    assert reply.usage == {"input_tokens": 50, "output_tokens": 20}


def test_huggingface_sends_assistant_and_tool_messages():
    session = FakeSession(
        FakeResponse(200, HF_TOOL_CALL), FakeResponse(200, {"choices": [{"message": {"content": "ok"}}]})
    )
    client = HuggingFaceClient("t", "m", session=session, sleep=lambda s: None)
    messages = [{"role": "user", "content": "x"}]
    messages.append(client.chat("S", messages, TOOLS).as_message())
    messages.append({"role": "tool", "tool_call_id": "abc", "name": "submit_triage", "content": "ok"})
    client.chat("S", messages, TOOLS)
    sent = session.posts[1]["json"]["messages"]
    assert sent[2]["role"] == "assistant" and sent[2]["tool_calls"][0]["function"]["name"] == "submit_triage"
    assert isinstance(sent[2]["tool_calls"][0]["function"]["arguments"], str)
    assert sent[3] == {"role": "tool", "tool_call_id": "abc", "name": "submit_triage", "content": "ok"}


# ------------------------------------------------------- retries visible ----


def test_timeout_is_configurable_and_defaults_to_90s():
    assert gemini(FakeResponse(200, GEMINI_TEXT))[0].timeout == 90
    session = FakeSession(FakeResponse(200, GEMINI_TEXT))
    GeminiClient("k", "m", session=session, timeout=45).chat("S", [{"role": "user", "content": "x"}], TOOLS)
    assert session.posts[0]["timeout"] == 45


def test_retries_are_recorded_on_the_llm_span_logged_and_counted(spans, caplog, recorded):
    from email_agent.telemetry import tracer

    client, session = gemini(
        requests.ReadTimeout("read timed out"), FakeResponse(429, {}), FakeResponse(200, GEMINI_TEXT)
    )
    with caplog.at_level(logging.INFO, logger="email_agent"), tracer.start_as_current_span("llm.chat"):
        reply = client.chat("S", [{"role": "user", "content": "x"}], TOOLS)
    assert reply.text == "Done." and len(session.posts) == 3
    (span,) = (s for s in spans.get_finished_spans() if s.name == "llm.chat")
    retries = [dict(e.attributes) for e in span.events if e.name == "llm.retry"]
    assert [(r["llm.retry.reason"], r["llm.retry.attempt"]) for r in retries] == [
        ("ReadTimeout", 1),
        ("rate_limited", 2),
    ]
    assert span.attributes["llm.attempts"] == 3
    logged = [r for r in caplog.records if getattr(r, "event", None) == "llm.retry"]
    assert [r.__dict__["llm.retry.reason"] for r in logged] == ["ReadTimeout", "rate_limited"]
    assert recorded("llm_retries", reason="ReadTimeout") == 1 and recorded("llm_retries", reason="rate_limited") == 1
