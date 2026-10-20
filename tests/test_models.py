import pytest
from pydantic import ValidationError

from email_agent.models import (
    Category,
    EmailResult,
    ExtractedFields,
    ReplyDraft,
    Status,
    Triage,
)


def make_triage(**overrides):
    data = dict(category="incident", confidence=0.9, priority="P1", summary="Payment API down")
    data.update(overrides)
    return Triage(**data)


def test_triage_accepts_valid_data():
    triage = make_triage()
    assert triage.category is Category.INCIDENT
    assert triage.fields == ExtractedFields()


@pytest.mark.parametrize("confidence", [-0.1, 1.5])
def test_triage_rejects_confidence_out_of_range(confidence):
    with pytest.raises(ValidationError):
        make_triage(confidence=confidence)


def test_triage_rejects_unknown_category():
    with pytest.raises(ValidationError):
        make_triage(category="lottery_win")


def test_triage_rejects_long_summary():
    with pytest.raises(ValidationError):
        make_triage(summary="x" * 301)


def test_extracted_fields_mask_long_numbers():
    fields = ExtractedFields(account_masked="123456789012")
    assert fields.account_masked == "XXXX9012"


def test_reply_body_length_is_capped():
    # The LLM may write 1200 chars (tools.SubmitReplyArgs); the rest is room for filled-in placeholders.
    ReplyDraft(subject="Re: hi", body="x" * 1400)
    with pytest.raises(ValidationError):
        ReplyDraft(subject="Re: hi", body="x" * 1501)


def test_ignored_result_cannot_carry_triage_or_draft():
    with pytest.raises(ValidationError):
        EmailResult(email_id="1", status=Status.IGNORED, triage=make_triage())


@pytest.mark.parametrize("status", [Status.FILED, Status.QUARANTINED, Status.NEEDS_REVIEW, Status.ERROR])
def test_only_drafted_or_escalated_may_have_a_draft(status):
    with pytest.raises(ValidationError):
        EmailResult(email_id="1", status=status, triage=make_triage(), draft=ReplyDraft(subject="Re: x", body="Hello"))


def test_drafted_result_requires_triage_and_draft():
    with pytest.raises(ValidationError):
        EmailResult(email_id="1", status=Status.DRAFTED)
    result = EmailResult(
        email_id="1", status=Status.DRAFTED, triage=make_triage(), draft=ReplyDraft(subject="Re: x", body="Hello")
    )
    assert result.draft.body == "Hello"
