"""What happens after the guardrails decide: log, hand off for review, label, send or save drafts.
Sending (AUTO_SEND, on by default) only happens for allowed categories with no guardrail
flags, at most max_sends per run and never twice for the same email; anything else stays a draft."""

import json
import re
from datetime import UTC, datetime
from pathlib import Path

from .models import Category, Email, EmailResult, Status
from .telemetry import drafts_saved, event, replies_sent, tracer

DRAFT_STATUSES = {Status.DRAFTED, Status.ESCALATED}
ACTION_FLAGS = {"sent", "already_sent", "draft_exists"}  # what was done, not guardrail findings
REVIEW_STATUSES = {Status.NEEDS_REVIEW, Status.QUARANTINED, Status.ESCALATED}


CLOSING_LINE = re.compile(
    r"((best|kind|warm|warmest) )?regards|thanks( and regards)?|thank you|(yours )?sincerely|cheers", re.I
)


def _drop_closing_before_signature(body: str) -> str:
    """The signature already has a closing ("Regards,"), so drop the LLM's own one right above it."""
    head, placeholder, tail = body.partition("{signature}")
    lines = head.rstrip().split("\n")
    removed = False
    while lines and CLOSING_LINE.fullmatch(lines[-1].strip().rstrip(",.!")):
        lines.pop()
        removed = True
    if not (placeholder and removed):
        return body
    return "\n".join(lines).rstrip() + "\n\n" + placeholder + tail


def fill_placeholders(body: str, ticket_id: str, sla: str, signature: str) -> str:
    """Values the LLM must never invent are inserted here. Unknown placeholders are removed."""
    body = _drop_closing_before_signature(body)
    if lead := sla.split(" ", 1)[0]:  # "within {sla}" with sla "within 2 business days": no "within within"
        body = re.sub(rf"\b{re.escape(lead)}\s+(?=\{{sla\}})", "", body, flags=re.I)
    body = body.replace("{ticket_id}", ticket_id).replace("{sla}", sla).replace("{signature}", signature)
    return re.sub(r"\{[a-z_]+\}", "", body)


def label_for(result: EmailResult) -> str | None:
    if result.status is Status.NO_REPLY:
        return "Triage/NoReply"
    if result.status is Status.NEEDS_REVIEW:
        return "Triage/NeedsReview"
    if result.status is Status.QUARANTINED:
        return "Triage/Quarantine"
    if result.triage:
        return f"Triage/{result.triage.category.value}"
    return None


class Actions:
    """Carries out a decided result: label, send or save a draft, and write the output logs."""

    def __init__(
        self,
        mailbox,
        output_dir: Path,
        save_drafts: bool,
        apply_labels: bool,
        sender=None,
        send_categories: frozenset[Category] = frozenset(),
        max_sends: int = 0,
    ):
        self.mailbox = mailbox
        self.output_dir = Path(output_dir)
        self.save_drafts = save_drafts
        self.apply_labels = apply_labels
        self.sender = sender  # None = never send
        self.send_categories = send_categories
        self.sends_left = max_sends
        self._sent: set[str] | None = None  # message ids from sent.jsonl, loaded on first use

    def apply(self, email: Email, result: EmailResult) -> EmailResult:
        """Label, send or save the draft, then log what actually happened. Safe to repeat for the same
        email: a reply is never sent twice, and an existing draft is never replaced or duplicated
        (a person may have edited it)."""
        if self.apply_labels and (label := label_for(result)):
            with tracer.start_as_current_span("mailbox.apply_label", attributes={"label": label}):
                self.mailbox.apply_label(email, label)
            event("label.applied", f"Email {email.id}: label {label}", **{"email.id": email.id, "label": label})
        sendable = self._sendable(email, result)
        if sendable and email.message_id in self._sent_ids():  # a rerun: the reply is already out
            result = result.model_copy(update={"guardrail_flags": result.guardrail_flags + ["already_sent"]})
        elif sendable and self.sends_left > 0:
            with tracer.start_as_current_span(
                "smtp.send", attributes={"triage.category": result.triage.category.value}
            ):
                self.sender.send(email, result.draft)
            self._record_sent(email, result)
            replies_sent.add(1, {"category": result.triage.category.value})
            event(
                "reply.sent",
                f"Email {email.id}: reply sent to {email.from_addr}",
                **{"email.id": email.id, "reply.to": email.from_addr, "email.category": result.triage.category.value},
            )
            result = result.model_copy(update={"guardrail_flags": result.guardrail_flags + ["sent"]})
        elif self.save_drafts and result.draft and result.status in DRAFT_STATUSES:
            with tracer.start_as_current_span("mailbox.save_draft") as span:
                created = self.mailbox.save_draft(email, result.draft)
                span.set_attribute("draft.created", created)
            if created:
                drafts_saved.add(1)
            event(
                "draft.saved" if created else "draft.exists",
                f"Email {email.id}: draft " + ("saved" if created else "already exists, left untouched"),
                **{"email.id": email.id},
            )
            if not created:
                result = result.model_copy(update={"guardrail_flags": result.guardrail_flags + ["draft_exists"]})
        self.log(result, email)
        return result

    def _sendable(self, email: Email, result: EmailResult) -> bool:
        return bool(
            self.sender
            and result.draft
            and result.triage
            and result.status in DRAFT_STATUSES
            and result.triage.category in self.send_categories
            and not result.guardrail_flags  # any doubt at all: a person decides
            and email.message_id
        )  # needed to make sure it is never sent twice

    def _sent_ids(self) -> set[str]:
        if self._sent is None:
            path = self.output_dir / "sent.jsonl"
            lines = path.read_text(encoding="utf-8").splitlines() if path.exists() else []
            self._sent = {json.loads(line)["message_id"] for line in lines}
        return self._sent

    def _record_sent(self, email: Email, result: EmailResult) -> None:
        self.sends_left -= 1
        self._sent_ids().add(email.message_id)
        self._append(
            "sent.jsonl",
            {
                "at": datetime.now(UTC).isoformat(),
                "email_id": email.id,
                "message_id": email.message_id,
                "to": email.from_addr,
                "subject": result.draft.subject,
                "category": result.triage.category.value,
            },
        )

    def log(self, result: EmailResult, email: Email | None = None) -> None:
        """results.jsonl gets every email; review_queue.jsonl gets what a human must look at."""
        record = {"at": datetime.now(UTC).isoformat(), "email_id": result.email_id, "status": result.status.value}
        if result.status is not Status.IGNORED:  # ignored emails keep nothing else
            if email:
                record.update(message_id=email.message_id, sender=email.from_addr, subject=email.subject)
            record.update(result.model_dump(mode="json", exclude={"email_id", "status"}))
        self._append("results.jsonl", record)
        if result.status in REVIEW_STATUSES:
            self._append("review_queue.jsonl", record)

    def _append(self, name: str, record: dict) -> None:
        self.output_dir.mkdir(parents=True, exist_ok=True)
        with open(self.output_dir / name, "a", encoding="utf-8") as f:
            f.write(json.dumps(record, ensure_ascii=False) + "\n")
