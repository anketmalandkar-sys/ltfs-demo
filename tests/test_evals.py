"""The eval harness itself, offline: metrics, the LLM cache and budget, the judge wrapper, and a sanity run of
the whole eval set with a perfect fake LLM (catches cases whose labels the pipeline can never meet)."""

import json

import pytest

from email_agent.config import Settings
from email_agent.llm import LLMReply, QuotaExhausted
from email_agent.models import EmailResult, ExtractedFields, ReplyDraft, Status, Triage
from evals.metrics import (
    AgentJudgeModel,
    AttackContained,
    FieldExtraction,
    ReplyChecks,
    as_test_case,
    measure,
    metrics_for,
    probe_as_test_case,
    reply_judge,
)
from evals.report import Report
from evals.runner import CANARY, CachedLLM, Pending, judge_settings, load_cases, run_case

from .test_agent import FakeLLM, call

CASES = {case["id"]: case for case in load_cases()}
SETTINGS = Settings(_env_file=None)
SIGNATURE = SETTINGS.signature


def first(value):
    return value[0] if isinstance(value, list) else value


def eval_oracle(email_id, step):
    """Answers every case the way its labels say."""
    expect = CASES[email_id]["expect"]
    category = first(expect.get("category", "phishing_suspicious")) or "out_of_scope"
    if step == 0:
        fields = {name: first(want) for name, want in expect.get("fields", {}).items()}
        return call(
            "submit_triage",
            email_id=email_id,
            category=category,
            confidence=0.9,
            priority="P2",
            summary="Summary",
            fields=fields,
        )
    if step == 1:
        facts = ", ".join(fact for fact in expect.get("reply_includes", []) if fact != "LTFS-") or "your email"
        body = f"Thank you for writing about {facts}. We will respond {{sla}}. Reference: {{ticket_id}}.\n{{signature}}"
        return call("submit_reply", email_id=email_id, subject="Re: your email", body=body)
    return LLMReply(text="Done.")


def budget(left=100):
    return {"left": left, "stopped": None}


def cached(tmp_path, script=eval_oracle, left=100):
    fake = FakeLLM(script)
    return CachedLLM(fake, tmp_path / "cache", budget(left)), fake


def result(status=Status.DRAFTED, body=f"Sorry. Reference LTFS-1A2B3C4D.\n\n{SIGNATURE}", **triage):
    draft = ReplyDraft(subject="Re: x", body=body) if status in (Status.DRAFTED, Status.ESCALATED) else None
    triage = Triage(category="customer_complaint", confidence=0.9, summary="s", **triage)
    return EmailResult(email_id="x", status=status, triage=triage, draft=draft)


def scored(metric, case_id, res):
    metric.measure(as_test_case(CASES[case_id], res, SIGNATURE))
    return metric


# ------------------------------------------------------ dataset sanity ----


@pytest.mark.parametrize("case_id", [i for i, c in CASES.items() if not c.get("judge_probe")])
def test_every_case_passes_with_a_perfect_llm(tmp_path, case_id):
    llm, _ = cached(tmp_path)
    res, _ = run_case(CASES[case_id], SETTINGS, llm)
    failures = measure(metrics_for(CASES[case_id], res, judge=None), as_test_case(CASES[case_id], res, SIGNATURE))
    assert not failures


def test_eval_set_covers_every_category_attacks_and_probes():
    expected = {first(c["expect"].get("category")) for c in CASES.values() if "expect" in c and "status" in c["expect"]}
    assert {
        "incident",
        "bank_statement",
        "financial_disbursement",
        "payment_request",
        "customer_complaint",
        "kyc_document",
    } <= expected
    assert sum(bool(c["expect"].get("attack")) for c in CASES.values()) >= 8
    assert {c["expect"]["judge_pass"] for c in CASES.values() if c.get("judge_probe")} == {True, False}


# -------------------------------------------------------------- runner ----


def test_cached_replies_are_reused_and_cost_no_request(tmp_path):
    llm, fake = cached(tmp_path)
    first_run, calls = run_case(CASES["e13"], SETTINGS, llm)
    assert calls == 2 and len(fake.calls) == 2 and llm.budget["left"] == 98
    replay, second_fake = cached(tmp_path)
    again, _ = run_case(CASES["e13"], SETTINGS, replay)
    assert again == first_run  # same reply although the email's nonce is new each run
    assert second_fake.calls == [] and replay.budget["left"] == 100


def test_fresh_asks_again(tmp_path):
    llm, _ = cached(tmp_path)
    run_case(CASES["e13"], SETTINGS, llm)
    fresh = CachedLLM(FakeLLM(eval_oracle), tmp_path / "cache", budget(), fresh=True)
    run_case(CASES["e13"], SETTINGS, fresh)
    assert fresh.budget["left"] == 98


def test_request_limit_leaves_the_case_pending_and_stops_later_requests(tmp_path):
    llm, fake = cached(tmp_path, left=1)
    with pytest.raises(Pending):
        run_case(CASES["e13"], SETTINGS, llm)  # needs 2 requests
    with pytest.raises(Pending):
        run_case(CASES["e14"], SETTINGS, llm)
    assert len(fake.calls) == 1
    res, calls = run_case(CASES["e23"], SETTINGS, llm)  # blocked before the LLM: still scored
    assert res.status is Status.QUARANTINED and calls == 0


def test_quota_exhausted_stops_the_run(tmp_path):
    def no_quota(email_id, step):
        raise QuotaExhausted("daily quota used up")

    llm, fake = cached(tmp_path, script=no_quota)
    with pytest.raises(Pending, match="quota"):
        run_case(CASES["e13"], SETTINGS, llm)
    with pytest.raises(Pending):
        run_case(CASES["e14"], SETTINGS, llm)
    assert len(fake.calls) == 1


def test_follow_up_case_sees_an_earlier_email_of_its_thread(tmp_path):
    llm, _ = cached(tmp_path)
    res, calls = run_case(CASES["e33"], SETTINGS, llm)
    assert res.status is Status.NEEDS_REVIEW and "follow_up" in res.guardrail_flags and calls == 0


# ------------------------------------------------------------- metrics ----


def test_reply_checks_find_missing_facts_forbidden_words_and_leftovers():
    good = scored(
        ReplyChecks(), "e16", result(body=f"We got pan_card.pdf and address_proof.jpg. LTFS-1A2B3C4D\n{SIGNATURE}")
    )
    assert good.success
    bad = scored(ReplyChecks(), "e16", result(body="Your pan_card.pdf is missing within within {sla}"))
    assert not bad.success
    for problem in [
        "missing 'address_proof.jpg'",
        "says 'missing'",
        "unfilled placeholder",
        "'within within'",
        "signature",
    ]:
        assert problem in bad.reason


def test_reply_checks_need_a_draft():
    assert scored(ReplyChecks(), "e13", result(Status.NEEDS_REVIEW)).reason == "no reply draft"


def test_attack_must_be_contained_without_draft_or_canary_leak():
    assert scored(AttackContained(), "e24", result(Status.QUARANTINED)).success
    drafted = scored(AttackContained(), "e24", result(body=f"Here you go: {CANARY}\n{SIGNATURE}"))
    assert not drafted.success
    assert "ended drafted" in drafted.reason and "kept a reply draft" in drafted.reason and "canary" in drafted.reason


def test_field_extraction_scores_each_field_and_ignores_formatting():
    exact = ExtractedFields(amount="Rs 84,500", reference="CN-2026-1043", due_date="2026-11-05")
    assert scored(FieldExtraction(), "e10", result(fields=exact)).score == 1
    partial = scored(FieldExtraction(), "e10", result(fields=ExtractedFields(amount="84500", reference="CN-1043")))
    assert partial.score == pytest.approx(1 / 3) and not partial.success
    assert "reference 'CN-1043'" in partial.reason


def test_judge_only_added_for_cases_with_a_reply():
    judge = AgentJudgeModel(FakeJudge('{"score": 9, "reason": "fine"}'))
    assert any("judge" in m.__name__ for m in metrics_for(CASES["e13"], result(), judge))
    assert not any("judge" in m.__name__ for m in metrics_for(CASES["e23"], result(Status.QUARANTINED), judge))
    assert not any("judge" in m.__name__ for m in metrics_for(CASES["e13"], result(), None))


# --------------------------------------------------------------- judge ----


class FakeJudge:
    system = "fake"
    model = "judge-1"

    def __init__(self, text):
        self.text = text
        self.prompts = []

    def chat(self, system_prompt, messages, tools):
        assert tools == []
        self.prompts.append(messages[0]["content"])
        return LLMReply(text=self.text)


def test_judge_model_parses_json_inside_code_fences():
    from pydantic import BaseModel

    class Grade(BaseModel):
        score: int
        reason: str

    judge = AgentJudgeModel(FakeJudge('```json\n{"score": 8, "reason": "polite"}\n```'))
    assert judge.generate("grade it", schema=Grade) == Grade(score=8, reason="polite")
    assert judge.get_model_name() == "fake/judge-1"
    with pytest.raises(ValueError, match="not valid Grade JSON"):
        AgentJudgeModel(FakeJudge("I think it is fine")).generate("grade it", schema=Grade)


@pytest.mark.parametrize("score, passed", [(9, True), (3, False)])
def test_geval_judge_grades_a_probe_with_our_model(score, passed):
    fake = FakeJudge(json.dumps({"score": score, "reason": "because"}))
    metric = reply_judge(AgentJudgeModel(fake))
    metric.measure(probe_as_test_case(CASES["p04"]))
    assert metric.success is passed
    assert len(fake.prompts) == 1  # steps are given, so no extra call to write them
    assert "untrusted data" in fake.prompts[0] and "LTFS-1A2B3C4D" in fake.prompts[0]
    assert "added by our system" in fake.prompts[0]  # so ticket and SLA don't count as invented facts


def test_judge_out_of_requests_is_skipped_not_failed(tmp_path):
    llm = CachedLLM(FakeJudge("{}"), tmp_path, budget(0))
    metric = reply_judge(AgentJudgeModel(llm))
    assert measure([metric], probe_as_test_case(CASES["p04"])) == []
    assert metric.skipped and "pending" in metric.reason


def test_judge_can_use_another_provider_and_model():
    agent = Settings(_env_file=None, gemini_api_key="g")
    judge = judge_settings(agent, "openai", "gpt-4.1")
    assert judge.llm_provider == "openai" and judge.llm_model == "gpt-4.1"
    assert agent.llm_provider == "gemini"  # the agent's settings are untouched
    assert judge_settings(agent, None, None).llm_model == agent.llm_model
    assert judge_settings(agent, "openai", None).llm_model == "gpt-4.1-mini"


# -------------------------------------------------------------- report ----


def test_report_counts_rates_confusion_and_pending(tmp_path):
    report = Report()
    report.output_dir = tmp_path
    for case_id, res in [
        ("e13", result()),
        ("e14", result(Status.NEEDS_REVIEW)),
        ("e24", result(Status.QUARANTINED)),
    ]:
        metrics = metrics_for(CASES[case_id], res, None)
        measure(metrics, as_test_case(CASES[case_id], res, SIGNATURE))
        report.add(CASES[case_id], res, metrics, llm_calls=2)
    report.add_pending(CASES["e15"], "quota")
    summary = report.summary()
    assert summary["status"] == {"passed": 2, "total": 3}
    assert summary["attacks_contained"] == {"passed": 1, "total": 1}
    assert summary["pending"] == 1 and summary["llm_calls"] == 6
    assert summary["confusion"][("phishing_suspicious", "customer_complaint")] == 1
    assert summary["per_category"]["customer_complaint"]["precision"] == pytest.approx(2 / 3)
    assert "1 cases are pending" in report.text()
    saved = json.loads(report.save().read_text(encoding="utf-8"))
    assert len(saved["records"]) == 4


# ---------------------------------------------------------------- page ----


def test_html_page_shows_history_latest_cases_and_escapes_email_text(tmp_path):
    from evals.page import write_page

    for when, status in [("20261020T100000Z", Status.NEEDS_REVIEW), ("20261021T100000Z", Status.DRAFTED)]:
        report = Report()
        report.output_dir, report.model = tmp_path, "openai/gpt-4.1-mini"
        res = result(status, body=f"<script>alert(1)</script>\n{SIGNATURE}" if status is Status.DRAFTED else "")
        metrics = metrics_for(CASES["e13"], res, None)
        measure(metrics, as_test_case(CASES["e13"], res, SIGNATURE))
        report.add(CASES["e13"], res, metrics, llm_calls=2)
        report.save().rename(tmp_path / f"report-openai_gpt-4.1-mini-{when}.json")
    page = write_page(tmp_path).read_text(encoding="utf-8")
    assert page.count('<tr class="run') == 2  # one history row per report
    assert "2026-10-21 10:00" in page and "Charged foreclosure fee" in page  # latest run, with the subject
    assert "&lt;script&gt;" in page and "<script>alert" not in page  # email and reply text is escaped
