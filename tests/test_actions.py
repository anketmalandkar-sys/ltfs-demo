import json

import pytest

from email_agent.actions import Actions, fill_placeholders
from email_agent.models import Category, Email, EmailResult, ReplyDraft, Status, Triage

EMAIL = Email(id="7", message_id="<m7@x>", from_addr="cust@x.com", subject="EMI issue", body="secret body")
TRIAGE = Triage(category="customer_complaint", confidence=0.9, summary="EMI issue")
DRAFT = ReplyDraft(subject="Re: EMI issue", body="Sorry")


class RecordingMailbox:
    def __init__(self):
        self.drafts, self.labels = [], []

    def save_draft(self, email, draft):
        if any(email_id == email.id for email_id, _ in self.drafts):
            return False
        self.drafts.append((email.id, draft.body))
        return True

    def apply_label(self, email, label):
        self.labels.append((email.id, label))


def read_jsonl(path):
    return [json.loads(line) for line in path.read_text().splitlines()]


def test_fill_placeholders_fills_known_and_removes_unknown():
    body = "Ticket {ticket_id}, reply {sla}. {foo}\n{signature}"
    assert fill_placeholders(body, "LTFS-1", "within 2 days", "Team") == "Ticket LTFS-1, reply within 2 days. \nTeam"


@pytest.mark.parametrize("before", ["within {sla}", "Within {sla}", "{sla}"])
def test_sla_lead_word_is_not_doubled(before):
    body = f"We will respond {before}."
    assert fill_placeholders(body, "T", "within 2 business days", "S") == "We will respond within 2 business days."


SIGNATURE = "Regards,\nCustomer Care Team"


@pytest.mark.parametrize(
    "body",
    [
        # what gemini-3.5-flash-lite wrote for m06
        "We acknowledge your query.\n\nBest regards,\n{signature}",
        "We acknowledge your query.\n\nKind regards,\n\n{signature}",
        "We acknowledge your query.\n\nThanks and regards\n{signature}",
        "We acknowledge your query.\n\nSincerely,\n{signature}",
        "We acknowledge your query.\n\n{signature}",
    ],
)
def test_no_double_sign_off(body):
    assert fill_placeholders(body, "T", "S", SIGNATURE) == "We acknowledge your query.\n\nRegards,\nCustomer Care Team"


def test_sign_off_words_inside_the_message_are_kept():
    body = "Thank you for your email.\nRegards to your team, we will follow up.\n{signature}"
    assert fill_placeholders(body, "T", "S", SIGNATURE) == (
        "Thank you for your email.\nRegards to your team, we will follow up.\nRegards,\nCustomer Care Team"
    )


def test_existing_draft_is_kept_and_flagged(tmp_path):
    box = RecordingMailbox()
    actions = Actions(box, tmp_path, save_drafts=True, apply_labels=False)
    first = actions.apply(EMAIL, EmailResult(email_id="7", status=Status.DRAFTED, triage=TRIAGE, draft=DRAFT))
    again = actions.apply(
        EMAIL,
        EmailResult(
            email_id="7",
            status=Status.DRAFTED,
            triage=TRIAGE,
            draft=ReplyDraft(subject="Re: EMI issue", body="Different"),
        ),
    )
    assert box.drafts == [("7", "Sorry")]
    assert "draft_exists" not in first.guardrail_flags
    assert "draft_exists" in again.guardrail_flags
    assert read_jsonl(tmp_path / "results.jsonl")[1]["guardrail_flags"] == ["draft_exists"]


def test_drafted_result_is_logged_and_draft_only_saved_when_enabled(tmp_path):
    box = RecordingMailbox()
    result = EmailResult(email_id="7", status=Status.DRAFTED, triage=TRIAGE, draft=DRAFT)
    Actions(box, tmp_path, save_drafts=False, apply_labels=False).apply(EMAIL, result)
    assert box.drafts == []
    Actions(box, tmp_path, save_drafts=True, apply_labels=False).apply(EMAIL, result)
    assert box.drafts == [("7", "Sorry")]
    records = read_jsonl(tmp_path / "results.jsonl")
    assert len(records) == 2 and records[0]["status"] == "drafted" and records[0]["subject"] == "EMI issue"
    assert "secret body" not in (tmp_path / "results.jsonl").read_text()
    assert not (tmp_path / "review_queue.jsonl").exists()


def test_review_statuses_go_to_review_queue(tmp_path):
    actions = Actions(RecordingMailbox(), tmp_path, save_drafts=True, apply_labels=False)
    for status in (Status.NEEDS_REVIEW, Status.QUARANTINED, Status.ESCALATED):
        actions.apply(EMAIL, EmailResult(email_id="7", status=status, triage=TRIAGE))
    assert [r["status"] for r in read_jsonl(tmp_path / "review_queue.jsonl")] == [
        "needs_review",
        "quarantined",
        "escalated",
    ]


def test_ignored_keeps_only_id_and_status(tmp_path):
    Actions(RecordingMailbox(), tmp_path, False, False).apply(
        EMAIL, EmailResult(email_id="7", status=Status.IGNORED, guardrail_flags=["hidden_text"])
    )
    record = read_jsonl(tmp_path / "results.jsonl")[0]
    assert set(record) == {"at", "email_id", "status"}


def test_error_without_email_logs_minimal_record(tmp_path):
    Actions(RecordingMailbox(), tmp_path, True, True).log(
        EmailResult(email_id="7", status=Status.ERROR, guardrail_flags=["error:LLMError"])
    )
    record = read_jsonl(tmp_path / "results.jsonl")[0]
    assert record["status"] == "error" and "subject" not in record


def test_labels(tmp_path):
    box = RecordingMailbox()
    actions = Actions(box, tmp_path, save_drafts=False, apply_labels=True)
    actions.apply(EMAIL, EmailResult(email_id="7", status=Status.DRAFTED, triage=TRIAGE, draft=DRAFT))
    actions.apply(EMAIL, EmailResult(email_id="7", status=Status.NEEDS_REVIEW, triage=TRIAGE))
    actions.apply(EMAIL, EmailResult(email_id="7", status=Status.QUARANTINED))
    actions.apply(EMAIL, EmailResult(email_id="7", status=Status.IGNORED))
    assert box.labels == [("7", "Triage/customer_complaint"), ("7", "Triage/NeedsReview"), ("7", "Triage/Quarantine")]


def test_actions_create_spans(tmp_path, spans):
    Actions(RecordingMailbox(), tmp_path, save_drafts=True, apply_labels=True).apply(
        EMAIL, EmailResult(email_id="7", status=Status.DRAFTED, triage=TRIAGE, draft=DRAFT)
    )
    assert {s.name for s in spans.get_finished_spans()} == {"mailbox.save_draft", "mailbox.apply_label"}


# ------------------------------------------------------------ sending ----


class RecordingSender:
    def __init__(self):
        self.sent = []

    def send(self, email, draft):
        self.sent.append((email.id, draft.body))


INCIDENT = Triage(category="incident", confidence=0.9, summary="Gateway down")


def sending(tmp_path, max_sends=5, categories=frozenset({Category.CUSTOMER_COMPLAINT})):
    box, sender = RecordingMailbox(), RecordingSender()
    actions = Actions(
        box,
        tmp_path,
        save_drafts=True,
        apply_labels=False,
        sender=sender,
        send_categories=categories,
        max_sends=max_sends,
    )
    return actions, box, sender


def test_allowed_clean_reply_is_sent_logged_and_not_drafted(tmp_path):
    actions, box, sender = sending(tmp_path)
    result = actions.apply(EMAIL, EmailResult(email_id="7", status=Status.DRAFTED, triage=TRIAGE, draft=DRAFT))
    assert sender.sent == [("7", "Sorry")] and box.drafts == []
    assert "sent" in result.guardrail_flags
    record = read_jsonl(tmp_path / "sent.jsonl")[0]
    assert (record["message_id"], record["to"], record["subject"]) == ("<m7@x>", "cust@x.com", "Re: EMI issue")


def test_category_not_allowed_stays_a_draft(tmp_path):
    actions, box, sender = sending(tmp_path)
    actions.apply(EMAIL, EmailResult(email_id="7", status=Status.ESCALATED, triage=INCIDENT, draft=DRAFT))
    assert sender.sent == [] and box.drafts == [("7", "Sorry")]


def test_allowed_escalation_is_sent_and_still_reviewed(tmp_path):
    actions, _, sender = sending(tmp_path, categories=frozenset({Category.INCIDENT}))
    actions.apply(EMAIL, EmailResult(email_id="7", status=Status.ESCALATED, triage=INCIDENT, draft=DRAFT))
    assert sender.sent == [("7", "Sorry")]
    assert read_jsonl(tmp_path / "review_queue.jsonl")[0]["status"] == "escalated"


def test_any_guardrail_flag_blocks_sending(tmp_path):
    actions, box, sender = sending(tmp_path)
    actions.apply(
        EMAIL,
        EmailResult(
            email_id="7",
            status=Status.DRAFTED,
            triage=TRIAGE,
            draft=DRAFT,
            guardrail_flags=["sender:reply_to_mismatch"],
        ),
    )
    assert sender.sent == [] and box.drafts == [("7", "Sorry")]


def test_never_sends_twice_even_across_runs(tmp_path):
    result = EmailResult(email_id="7", status=Status.DRAFTED, triage=TRIAGE, draft=DRAFT)
    actions, _, sender = sending(tmp_path)
    actions.apply(EMAIL, result)
    actions.apply(EMAIL, result)
    again, box, sender2 = sending(tmp_path)  # a new run reads sent.jsonl
    again.apply(EMAIL, result)
    assert len(sender.sent) == 1 and sender2.sent == [] and box.drafts == []


def test_send_limit_per_run_falls_back_to_drafts(tmp_path):
    actions, box, sender = sending(tmp_path, max_sends=1)
    for i in ("1", "2"):
        email = EMAIL.model_copy(update={"id": i, "message_id": f"<m{i}@x>"})
        actions.apply(email, EmailResult(email_id=i, status=Status.DRAFTED, triage=TRIAGE, draft=DRAFT))
    assert [s[0] for s in sender.sent] == ["1"] and box.drafts == [("2", "Sorry")]


def test_email_without_message_id_is_never_sent(tmp_path):
    actions, box, sender = sending(tmp_path)
    actions.apply(
        EMAIL.model_copy(update={"message_id": ""}),
        EmailResult(email_id="7", status=Status.DRAFTED, triage=TRIAGE, draft=DRAFT),
    )
    assert sender.sent == [] and box.drafts == [("7", "Sorry")]


def test_sends_drafts_and_labels_are_counted_and_logged(tmp_path, recorded, caplog):
    actions, box, _ = sending(tmp_path)
    actions.apply_labels = True
    with caplog.at_level("INFO", logger="email_agent"):
        actions.apply(EMAIL, EmailResult(email_id="7", status=Status.DRAFTED, triage=TRIAGE, draft=DRAFT))
        actions.apply(
            EMAIL.model_copy(update={"id": "8", "message_id": "<m8@x>"}),
            EmailResult(email_id="8", status=Status.ESCALATED, triage=INCIDENT, draft=DRAFT),
        )
    assert recorded("replies_sent", category="customer_complaint") == 1
    assert recorded("drafts_saved") == 1
    events = [getattr(r, "event", None) for r in caplog.records]
    assert events.count("label.applied") == 2 and "reply.sent" in events and "draft.saved" in events
    sent = next(r for r in caplog.records if getattr(r, "event", None) == "reply.sent")
    assert sent.__dict__["reply.to"] == "cust@x.com"


def test_no_reply_emails_get_their_own_label_and_never_a_draft(tmp_path):
    box = RecordingMailbox()
    Actions(box, tmp_path, save_drafts=True, apply_labels=True).apply(
        EMAIL, EmailResult(email_id="7", status=Status.NO_REPLY, guardrail_flags=["courtesy_reply"])
    )
    assert box.labels == [("7", "Triage/NoReply")] and box.drafts == []
    assert not (tmp_path / "review_queue.jsonl").exists()
