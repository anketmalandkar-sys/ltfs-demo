import json
import re
from pathlib import Path

import pytest

from email_agent.actions import Actions
from email_agent.agent import Agent, build_system_prompt
from email_agent.config import Settings
from email_agent.llm import FatalLLMError, LLMError, LLMReply, QuotaExhausted, ToolCall
from email_agent.mailbox import MockMailbox
from email_agent.models import Category, Status
from email_agent.state import State

INBOX = Path(__file__).parent.parent / "data" / "mock_inbox.json"
EXPECTED = {item["id"]: item for item in json.loads(INBOX.read_text(encoding="utf-8"))}
GROUNDED_REPLY = (
    "Thank you for your email. We have noted it and will respond {sla}. Reference: {ticket_id}.\n{signature}"
)


def call(name, **args):
    return LLMReply(tool_calls=[ToolCall(id=f"c-{name}", name=name, args=args)])


class FakeLLM:
    """Plays the LLM. `script(email_id, step)` returns the reply for that step."""

    system = "fake"
    model = "fake-1"

    def __init__(self, script):
        self.script = script
        self.calls = []

    def chat(self, system_prompt, messages, tools):
        email_id = re.search(r"id '([^']+)'", messages[0]["content"]).group(1)
        step = sum(1 for m in messages if m["role"] == "assistant")
        self.calls.append((email_id, step, list(messages)))  # copy: the agent keeps appending
        return self.script(email_id, step)


def oracle(email_id, step):
    """A well-behaved LLM that knows the right category for each mock email."""
    category = EXPECTED[email_id]["expected_category"] or "out_of_scope"
    if step == 0:
        return call(
            "submit_triage", email_id=email_id, category=category, confidence=0.9, priority="P2", summary="Summary"
        )
    if step == 1:
        return call("submit_reply", email_id=email_id, subject="Re: your email", body=GROUNDED_REPLY)
    return LLMReply(text="Done.")


def make_agent(tmp_path, script=oracle, inbox=INBOX, save_drafts=True, **settings):
    settings = Settings(_env_file=None, output_dir=tmp_path, **settings)
    mailbox = MockMailbox(inbox, tmp_path / "drafts")
    llm = FakeLLM(script)
    agent = Agent(
        settings,
        mailbox,
        llm,
        State(tmp_path / "state.json"),
        Actions(mailbox, tmp_path, save_drafts=save_drafts, apply_labels=False),
        own_address="triage@ltfs-demo.local",
        canary="CANARY-xyz",
    )
    return agent, llm


def single_email_inbox(tmp_path, email_id):
    path = tmp_path / "one.json"
    path.write_text(json.dumps([EXPECTED[email_id]]), encoding="utf-8")
    return path


def by_id(report):
    return {r.email_id: r for r in report.results}


# ------------------------------------------------------------ end to end ----


def test_every_mock_email_reaches_its_expected_status(tmp_path):
    agent, _ = make_agent(tmp_path)
    results = by_id(agent.run(limit=50))
    assert len(results) == len(EXPECTED)
    actual = {i: r.status.value for i, r in results.items()}
    expected = {i: item["expected_status"] for i, item in EXPECTED.items()}
    assert actual == expected


def test_attacks_never_reach_the_llm_and_never_get_a_draft(tmp_path):
    agent, llm = make_agent(tmp_path)
    results = by_id(agent.run(limit=50))
    attacks = [i for i, item in EXPECTED.items() if "attack" in item]
    assert len(attacks) == 6
    called = {email_id for email_id, _, _ in llm.calls}
    for email_id in attacks:
        assert email_id not in called
        assert results[email_id].draft is None
        assert results[email_id].guardrail_flags


def test_prefiltered_emails_cost_no_llm_calls(tmp_path):
    agent, llm = make_agent(tmp_path)
    agent.run(limit=50)
    called = {email_id for email_id, _, _ in llm.calls}
    assert not called & {"m12", "m13", "m21", "m22"}


def test_drafts_get_code_filled_placeholders_and_are_saved(tmp_path):
    agent, _ = make_agent(tmp_path)
    draft = by_id(agent.run(limit=50))["m08"].draft
    assert re.search(r"Reference: LTFS-[0-9A-F]{8}\.", draft.body)
    assert "within 2 business days" in draft.body and "Customer Care Team" in draft.body
    assert "{" not in draft.body
    assert (tmp_path / "drafts" / "m08.eml").exists()
    assert not (tmp_path / "drafts" / "m15.eml").exists()


def test_only_reprocesses_the_given_emails_even_if_done(tmp_path):
    agent, _ = make_agent(tmp_path)
    agent.run(limit=50)
    agent, llm = make_agent(tmp_path)
    results = agent.run(limit=50, only={"m06"}).results
    # Not caught as a follow-up, even though its thread was already handled
    assert [(r.email_id, r.status) for r in results] == [("m06", Status.DRAFTED)]
    assert {email_id for email_id, _, _ in llm.calls} == {"m06"}


def test_reprocessing_is_idempotent(tmp_path):
    # Ticket ids come only from the email (LTFS-<8 hex>, no date), so any day gives the same one.
    agent, _ = make_agent(tmp_path, inbox=single_email_inbox(tmp_path, "m08"))
    first = agent.run(limit=1).results[0]
    saved = (tmp_path / "drafts" / "m08.eml").read_bytes()

    agent, _ = make_agent(tmp_path, inbox=single_email_inbox(tmp_path, "m08"))
    second = agent.run(limit=1, only={"m08"}).results[0]

    assert second.draft.body == first.draft.body  # same ticket id
    assert (tmp_path / "drafts" / "m08.eml").read_bytes() == saved  # no new draft
    assert "draft_exists" in second.guardrail_flags and "draft_exists" not in first.guardrail_flags


def test_second_run_skips_processed_emails(tmp_path):
    agent, _ = make_agent(tmp_path)
    agent.run(limit=50)
    agent2, llm2 = make_agent(tmp_path)
    assert agent2.run(limit=50).results == []
    assert llm2.calls == []


def test_limit_counts_only_unprocessed_emails(tmp_path):
    # Done emails stay unread, so they must not use up the limit and starve the rest
    agent, _ = make_agent(tmp_path)
    first = [r.email_id for r in agent.run(limit=3).results]
    agent2, _ = make_agent(tmp_path)
    second = [r.email_id for r in agent2.run(limit=3).results]
    assert len(second) == 3 and not set(first) & set(second)


# ------------------------------------------------------ misbehaving LLMs ----


def test_llm_that_never_calls_tools_is_nudged_once_then_review(tmp_path):
    agent, llm = make_agent(
        tmp_path,
        script=lambda i, s: LLMReply(text="I think it is a complaint."),
        inbox=single_email_inbox(tmp_path, "m08"),
    )
    result = agent.run(limit=1).results[0]
    assert result.status is Status.NEEDS_REVIEW and "no_triage" in result.guardrail_flags
    assert len(llm.calls) == 2
    assert "must call a tool" in llm.calls[1][2][-1]["content"]


def test_unknown_tool_and_wrong_email_id_are_refused_and_reported_back(tmp_path):
    def script(email_id, step):
        if step == 0:
            return LLMReply(
                tool_calls=[
                    ToolCall(id="a", name="send_email", args={"to": "x@y.com"}),
                    ToolCall(id="b", name="submit_reply", args={"email_id": "m01", "subject": "x", "body": "x"}),
                ]
            )
        return oracle(email_id, step - 1)

    agent, llm = make_agent(tmp_path, script=script, inbox=single_email_inbox(tmp_path, "m08"))
    result = agent.run(limit=1).results[0]
    tool_messages = [m for m in llm.calls[1][2] if m["role"] == "tool"]
    assert "unknown tool" in tool_messages[0]["content"]
    assert "email_id must be 'm08'" in tool_messages[1]["content"]
    assert result.status is Status.DRAFTED


def test_max_steps_stops_a_looping_llm(tmp_path):
    agent, llm = make_agent(
        tmp_path,
        script=lambda i, s: call("submit_reply", email_id=i, subject="Re: x", body="Hi"),  # never triaged
        inbox=single_email_inbox(tmp_path, "m08"),
        max_steps=4,
    )
    result = agent.run(limit=1).results[0]
    assert len(llm.calls) == 4 and result.status is Status.NEEDS_REVIEW


def test_ungrounded_reply_is_held_for_review(tmp_path):
    def script(email_id, step):
        if step == 1:
            return call(
                "submit_reply",
                email_id=email_id,
                subject="Re: x",
                body="We confirm Rs 9,00,000 was disbursed. {signature}",
            )
        return oracle(email_id, step)

    agent, _ = make_agent(tmp_path, script=script, inbox=single_email_inbox(tmp_path, "m05"))
    result = agent.run(limit=1).results[0]
    assert result.status is Status.NEEDS_REVIEW and result.draft is None
    assert "ungrounded_number" in result.guardrail_flags
    assert not (tmp_path / "drafts").exists()


def test_unverified_payment_status_is_held_for_review(tmp_path):
    def script(email_id, step):  # replays what gemini-3.5-flash-lite wrote for m06
        if step == 1:
            return call(
                "submit_reply",
                email_id=email_id,
                subject="Re: Disbursement confirmation",
                body=(
                    "We confirm that the disbursement amount of INR 85,000 for loan application APP-7781 "
                    "is being processed. {signature}"
                ),
            )
        return oracle(email_id, step)

    agent, _ = make_agent(tmp_path, script=script, inbox=single_email_inbox(tmp_path, "m06"))
    result = agent.run(limit=1).results[0]
    assert result.status is Status.NEEDS_REVIEW and result.draft is None
    assert "unverified_status_claim" in result.guardrail_flags
    assert not (tmp_path / "drafts").exists()


def test_llm_error_is_retried_then_sent_to_review(tmp_path):
    def broken(email_id, step):
        raise LLMError("gemini returned HTTP 503")

    inbox = single_email_inbox(tmp_path, "m08")
    statuses = []
    for _ in range(3):
        agent, _ = make_agent(tmp_path, script=broken, inbox=inbox, max_attempts=3)
        statuses += [r.status for r in agent.run(limit=1).results]
    assert statuses == [Status.ERROR, Status.ERROR, Status.NEEDS_REVIEW]
    agent, llm = make_agent(tmp_path, script=broken, inbox=inbox, max_attempts=3)
    assert agent.run(limit=1).results == [] and llm.calls == []


@pytest.mark.parametrize(
    "error",
    [
        QuotaExhausted("gemini quota exhausted; retry in 58055s"),
        FatalLLMError("gemini returned HTTP 404: model is no longer available"),
    ],
)
def test_fatal_llm_error_stops_the_run_and_costs_no_attempt(tmp_path, spans, error):
    def broken(email_id, step):
        raise error

    agent, llm = make_agent(tmp_path, script=broken, max_attempts=1)
    results = agent.run(limit=50).results
    assert [(r.email_id, r.status) for r in results] == [("m01", Status.ERROR)]
    assert results[0].guardrail_flags == [f"error:{type(error).__name__}"]
    assert len(llm.calls) == 1
    assert agent.stopped_reason == str(error)
    run_span = next(s for s in spans.get_finished_spans() if s.name == "agent.run")
    assert "run_stopped" in [e.name for e in run_span.events]

    # Even with max_attempts=1, m01 is retried normally once the problem is fixed
    agent, _ = make_agent(tmp_path, max_attempts=1)
    assert by_id(agent.run(limit=50))["m01"].status is Status.ESCALATED


def test_budget_stops_the_run(tmp_path):
    agent, llm = make_agent(tmp_path, max_llm_calls_per_run=2)
    results = agent.run(limit=50).results
    assert len(llm.calls) == 2
    assert [r.email_id for r in results] == ["m01"]


# ------------------------------------------------------- prompt & tracing ----


def test_system_prompt_has_rules_categories_and_canary_but_no_email():
    prompt = build_system_prompt("CANARY-xyz")
    for category in Category:
        assert category.value in prompt
    assert "out_of_scope" in prompt and "CANARY-xyz" in prompt
    assert "untrusted" in prompt
    assert "Never state the status of a payment" in prompt
    assert "the team will confirm the status" in prompt  # disbursement guidance
    assert "{signature} already includes the closing" in prompt


def test_email_content_reaches_llm_only_inside_nonce_tags(tmp_path):
    agent, llm = make_agent(tmp_path, inbox=single_email_inbox(tmp_path, "m08"))
    agent.run(limit=1)
    first_user = llm.calls[-1][2][0]["content"]
    match = re.search(r'<email_content nonce="([0-9a-f]{16})">.*</email_content nonce="\1">', first_user, re.S)
    assert match and "deducted twice on 5 Oct" in match.group(0)
    assert "deducted twice" not in first_user.replace(match.group(0), "")


def test_spans(tmp_path, spans):
    agent, _ = make_agent(tmp_path, inbox=single_email_inbox(tmp_path, "m08"))
    agent.run(limit=1)
    finished = spans.get_finished_spans()
    names = [s.name for s in finished]
    assert names.count("agent.run") == 1 and names.count("email.process") == 1
    assert names.count("llm.chat") == 2
    assert {"tool.submit_triage", "tool.submit_reply"} <= set(names)
    process = next(s for s in finished if s.name == "email.process")
    assert process.attributes["triage.status"] == "drafted"
    assert process.attributes["triage.category"] == "customer_complaint"
    assert process.attributes["email.sender_domain"] == "gmail.com"
    llm_span = next(s for s in finished if s.name == "llm.chat")
    assert llm_span.attributes["gen_ai.system"] == "fake"
    dump = repr([(s.attributes, [e.attributes for e in s.events]) for s in finished])
    assert "deducted twice" not in dump and "CANARY" not in dump


def test_guardrail_flags_become_span_events(tmp_path, spans):
    agent, _ = make_agent(tmp_path, inbox=single_email_inbox(tmp_path, "m15"))
    agent.run(limit=1)
    process = next(s for s in spans.get_finished_spans() if s.name == "email.process")
    assert "guardrail.injection:ignore_instructions" in [e.name for e in process.events]


# ---------------------------------------------------------- observability ----


def llm_events(spans, name):
    return [
        e.attributes for s in spans.get_finished_spans() if s.name == "llm.chat" for e in s.events if e.name == name
    ]


def test_captured_llm_request_and_response_are_on_spans_and_in_logs(tmp_path, spans, caplog):
    agent, _ = make_agent(tmp_path, inbox=single_email_inbox(tmp_path, "m08"), otel_capture_content=True)
    with caplog.at_level("INFO", logger="email_agent"):
        agent.run(limit=1)
    requests = [e["gen_ai.prompt"] for e in llm_events(spans, "gen_ai.request")]
    responses = [e["gen_ai.completion"] for e in llm_events(spans, "gen_ai.response")]
    assert len(requests) == len(responses) == 2
    assert "You triage emails" in requests[0]  # system prompt, raw
    assert "deducted twice" in requests[0]  # the email text, in the first user message
    assert '"submit_triage"' in responses[0] and '"customer_complaint"' in responses[0]
    logged = [r for r in caplog.records if getattr(r, "event", None) in ("llm.request", "llm.response")]
    # content goes in the log line itself, so Loki's line filter (|= "...") finds it
    assert len(logged) == 4 and "deducted twice" in logged[0].getMessage()
    assert '"submit_triage"' in logged[1].getMessage()
    assert logged[0].__dict__["email.id"] == "m08"


def test_without_capture_no_content_in_logs(tmp_path, caplog):
    agent, _ = make_agent(tmp_path, inbox=single_email_inbox(tmp_path, "m08"))
    with caplog.at_level("INFO", logger="email_agent"):
        agent.run(limit=1)
    dump = repr([r.__dict__ | {"text": r.getMessage()} for r in caplog.records])
    assert "deducted twice" not in dump and "CANARY" not in dump
    # LLM calls are still logged (for counts, tokens, latency) - just without the text
    responses = [r for r in caplog.records if getattr(r, "event", None) == "llm.response"]
    assert len(responses) == 2 and responses[0].getMessage() == "LLM response: email m08, step 0"
    assert {"llm.input_tokens", "llm.output_tokens", "llm.duration_s"} <= set(responses[0].__dict__)


def test_failed_llm_call_is_logged(tmp_path, caplog):
    def broken(email_id, step):
        raise LLMError("gemini returned HTTP 500")

    agent, _ = make_agent(tmp_path, script=broken, inbox=single_email_inbox(tmp_path, "m08"))
    with caplog.at_level("INFO", logger="email_agent"):
        agent.run(limit=1)
    (error,) = (r for r in caplog.records if getattr(r, "event", None) == "llm.error")
    assert error.__dict__["llm.outcome"] == "error" and error.__dict__["error.type"] == "LLMError"


def test_each_guardrail_flag_is_logged_but_actions_are_not_flags(tmp_path, caplog, recorded):
    agent, _ = make_agent(tmp_path, inbox=single_email_inbox(tmp_path, "m15"))
    with caplog.at_level("INFO", logger="email_agent"):
        agent.run(limit=1)
    flags = [r.__dict__["guardrail.flag"] for r in caplog.records if getattr(r, "event", None) == "guardrail.flag"]
    assert "injection:ignore_instructions" in flags
    assert recorded("guardrail_flags", flag="injection:ignore_instructions") == 1


def test_sent_is_an_action_not_a_guardrail_flag(tmp_path, caplog, recorded):
    agent, _ = make_agent(tmp_path, inbox=single_email_inbox(tmp_path, "m08"))
    agent.actions.sender = type("S", (), {"send": lambda self, email, draft: None})()
    agent.actions.send_categories, agent.actions.sends_left = frozenset({Category.CUSTOMER_COMPLAINT}), 1
    with caplog.at_level("INFO", logger="email_agent"):
        result = agent.run(limit=1).results[0]
    assert "sent" in result.guardrail_flags
    assert recorded("guardrail_flags") == 0
    assert not [r for r in caplog.records if getattr(r, "event", None) == "guardrail.flag"]


def test_each_step_is_logged_with_metadata(tmp_path, caplog):
    agent, _ = make_agent(tmp_path, inbox=single_email_inbox(tmp_path, "m08"))
    with caplog.at_level("INFO", logger="email_agent"):
        agent.run(limit=1)
    events = [getattr(r, "event", None) for r in caplog.records]
    assert events.count("tool.call") == 2
    assert {"email.received", "email.decided"} <= set(events)
    decided = next(r for r in caplog.records if getattr(r, "event", None) == "email.decided")
    assert (decided.__dict__["email.status"], decided.__dict__["email.category"]) == ("drafted", "customer_complaint")


def test_metrics_for_emails_llm_and_guardrails(tmp_path, recorded):
    agent, _ = make_agent(tmp_path)
    agent.run(limit=50)
    assert recorded("emails_processed") == len(EXPECTED)
    assert recorded("emails_processed", status="drafted", category="customer_complaint") >= 1
    assert recorded("guardrail_flags", flag="injection:ignore_instructions") >= 1
    assert recorded("llm_requests", provider="fake", outcome="ok") > 0
    assert recorded("llm_request_duration", provider="fake") == recorded("llm_requests", provider="fake")


def test_llm_failures_are_counted(tmp_path, recorded):
    def broken(email_id, step):
        raise LLMError("gemini returned HTTP 500")

    agent, _ = make_agent(tmp_path, script=broken, inbox=single_email_inbox(tmp_path, "m08"))
    agent.run(limit=1)
    assert recorded("llm_requests", outcome="error") == 1


@pytest.mark.parametrize("capture", [True, False])
def test_decision_log_carries_subject_and_sender_only_with_capture(tmp_path, caplog, capture):
    agent, _ = make_agent(tmp_path, inbox=single_email_inbox(tmp_path, "m08"), otel_capture_content=capture)
    with caplog.at_level("INFO", logger="email_agent"):
        agent.run(limit=1)
    decided = next(r for r in caplog.records if getattr(r, "event", None) == "email.decided").__dict__
    assert decided["email.sender_domain"] == "gmail.com"
    assert ("email.subject" in decided and "email.from" in decided) is capture


@pytest.mark.parametrize("capture", [True, False])
def test_process_span_carries_subject_and_sender_only_with_capture(tmp_path, spans, capture):
    agent, _ = make_agent(tmp_path, inbox=single_email_inbox(tmp_path, "m08"), otel_capture_content=capture)
    agent.run(limit=1)
    process = next(s for s in spans.get_finished_spans() if s.name == "email.process")
    assert ("email.subject" in process.attributes and "email.from" in process.attributes) is capture
    assert process.attributes["triage.priority"] == "P2"


def with_usage(email_id, step):
    reply = oracle(email_id, step)
    return reply.model_copy(update={"usage": {"input_tokens": 1000, "output_tokens": 200}})


def test_tokens_and_estimated_cost_per_call_and_per_run(tmp_path, spans, caplog, recorded):
    agent, _ = make_agent(
        tmp_path,
        script=with_usage,
        inbox=single_email_inbox(tmp_path, "m08"),
        llm_input_price_per_mtok=0.10,
        llm_output_price_per_mtok=0.40,
    )
    with caplog.at_level("INFO", logger="email_agent"):
        agent.run(limit=1)
    per_call = 1000 * 0.10 / 1e6 + 200 * 0.40 / 1e6  # 0.00018 USD
    responses = [r.__dict__ for r in caplog.records if getattr(r, "event", None) == "llm.response"]
    assert responses[0]["llm.cost_usd"] == pytest.approx(per_call)
    llm_span = next(s for s in spans.get_finished_spans() if s.name == "llm.chat")
    assert llm_span.attributes["gen_ai.usage.cost_usd"] == pytest.approx(per_call)
    assert recorded("llm_cost", provider="fake") == pytest.approx(2 * per_call)
    assert (agent.tokens["input"], agent.tokens["output"]) == (2000, 400)
    assert agent.cost_usd == pytest.approx(2 * per_call)


def test_cost_is_zero_without_prices(tmp_path, caplog):
    agent, _ = make_agent(tmp_path, script=with_usage, inbox=single_email_inbox(tmp_path, "m08"))
    with caplog.at_level("INFO", logger="email_agent"):
        agent.run(limit=1)
    assert agent.cost_usd == 0 and agent.tokens["input"] == 2000


def test_thank_you_reply_is_recorded_but_never_answered(tmp_path):
    thanks = dict(
        EXPECTED["m08"],
        id="t1",
        message_id="<t1@gmail.com>",
        subject="Re: EMI deducted twice",
        body="Thank you so much for the quick reply!\n\nRegards,\nRavi",
    )
    path = tmp_path / "inbox.json"
    path.write_text(json.dumps([EXPECTED["m08"], thanks]), encoding="utf-8")
    agent, llm = make_agent(tmp_path, inbox=path)
    results = by_id(agent.run(limit=10))
    assert results["t1"].status is Status.NO_REPLY and results["t1"].guardrail_flags == ["courtesy_reply"]
    assert results["t1"].draft is None
    assert {email_id for email_id, _, _ in llm.calls} == {"m08"}  # no LLM call for the thank-you
    assert not (tmp_path / "drafts" / "t1.eml").exists()
    # recorded in full (unlike ignored emails), so it shows up in results and on the dashboard
    record = [json.loads(line) for line in (tmp_path / "results.jsonl").read_text().splitlines()][-1]
    assert record["status"] == "no_reply" and record["subject"] == "Re: EMI deducted twice"


def test_kyc_guidance_never_reports_missing_attachments_or_invents_documents():
    prompt = build_system_prompt("C")
    assert "never say that attachments are missing" in prompt
    assert "Never list documents yourself" in prompt


def test_prompt_forbids_claiming_actions():
    prompt = build_system_prompt("C")
    assert "forwarded" in prompt and "notified" in prompt
