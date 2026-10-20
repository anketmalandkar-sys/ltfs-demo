"""Data models shared by every part of the agent."""

import re
from datetime import UTC, datetime
from enum import Enum

from pydantic import BaseModel, Field, field_validator, model_validator


class Category(str, Enum):
    INCIDENT = "incident"
    BANK_STATEMENT = "bank_statement"
    FINANCIAL_DISBURSEMENT = "financial_disbursement"
    PAYMENT_REQUEST = "payment_request"
    CUSTOMER_COMPLAINT = "customer_complaint"
    KYC_DOCUMENT = "kyc_document"
    PHISHING_SUSPICIOUS = "phishing_suspicious"


class Status(str, Enum):
    DRAFTED = "drafted"
    FILED = "filed"
    ESCALATED = "escalated"
    QUARANTINED = "quarantined"
    IGNORED = "ignored"
    NO_REPLY = "no_reply"  # recorded and labelled, but never answered (e.g. a "thank you")
    NEEDS_REVIEW = "needs_review"
    ERROR = "error"


class Priority(str, Enum):
    P1 = "P1"
    P2 = "P2"
    P3 = "P3"
    P4 = "P4"


class Email(BaseModel):
    id: str
    message_id: str = ""
    thread_id: str = ""
    from_name: str = ""
    from_addr: str = ""
    reply_to: str = ""
    subject: str = ""
    date: str = ""
    body: str = ""
    headers: dict[str, str] = {}
    attachment_names: list[str] = []

    @property
    def sender_domain(self) -> str:
        return self.from_addr.rpartition("@")[2].lower()


def mask_digits(text: str) -> str:
    """Replace any run of 9+ digits with XXXX + last 4 digits."""
    return re.sub(r"\d{9,}", lambda m: "XXXX" + m.group()[-4:], text)


class ExtractedFields(BaseModel):
    amount: str | None = None
    currency: str | None = None
    reference: str | None = None
    account_masked: str | None = None
    period: str | None = None
    due_date: str | None = None

    @field_validator("*")
    @classmethod
    def no_full_numbers(cls, value: str | None) -> str | None:
        return mask_digits(value)[:100] if value else value


class Triage(BaseModel):
    category: Category
    confidence: float = Field(ge=0, le=1)
    priority: Priority | None = None
    summary: str = Field(max_length=300)
    fields: ExtractedFields = ExtractedFields()


class ReplyDraft(BaseModel):
    subject: str = Field(min_length=1, max_length=200)
    body: str = Field(min_length=1, max_length=1500)


class EmailResult(BaseModel):
    email_id: str
    status: Status
    triage: Triage | None = None
    draft: ReplyDraft | None = None
    guardrail_flags: list[str] = []

    @model_validator(mode="after")
    def consistent_status(self) -> "EmailResult":
        if self.status is Status.IGNORED and (self.triage or self.draft):
            raise ValueError("ignored emails keep no triage or draft")
        if self.draft and self.status not in (Status.DRAFTED, Status.ESCALATED):
            raise ValueError(f"status {self.status.value} cannot carry a draft")
        if self.status is Status.DRAFTED and not (self.triage and self.draft):
            raise ValueError("drafted emails need a triage and a draft")
        return self


class RunReport(BaseModel):
    started_at: datetime = Field(default_factory=lambda: datetime.now(UTC))
    provider: str
    model: str
    results: list[EmailResult] = []
