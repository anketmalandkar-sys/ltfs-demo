"""Deterministic safety checks. Every email is treated as untrusted input.

Layers (in the order the agent uses them):
  1. sanitize()        - clean the email before any model sees it
  2. is_auto_or_self() / is_irrelevant() - cheap pre-filters, no LLM call
  3. inspect()         - prompt-injection and sender-spoofing signals
  4. check_result()    - final say over the LLM's triage and reply
"""

import re
import unicodedata
from html.parser import HTMLParser

from pydantic import BaseModel

from .models import Category, Email, EmailResult, Status, mask_digits

# ---------------------------------------------------------------- input ----

INVISIBLE_CHARS = re.compile("[​-‏‪-‮⁠-⁤﻿\U000e0000-\U000e007f]")
HTML_HINT = re.compile(r"<(html|body|p|div|span|br|table|font)\b", re.I)
HIDDEN_STYLE = re.compile(
    r"display\s*:\s*none|visibility\s*:\s*hidden|font-size\s*:\s*0(px|pt|em|%)?\s*(;|$)"
    r"|(?<!-)color\s*:\s*(#fff\b|#ffffff\b|white\b)",
    re.I,
)
VOID_TAGS = {"br", "img", "hr", "meta", "link", "input", "wbr", "source", "area", "base", "col", "embed"}
BLOCK_TAGS = {"p", "div", "br", "li", "tr", "table", "h1", "h2", "h3", "h4", "h5", "h6"}
SKIP_TAGS = {"script", "style", "head", "title"}

QUOTE_HEADER = re.compile(
    r"^(On .{0,200}wrote:|-{2,}\s*Original Message\s*-{2,}|From: .+\n(Sent|Date): .+)", re.M | re.I
)
FORWARD_HEADER = re.compile(r"^-{2,}\s*Forwarded message\s*-{2,}", re.M | re.I)

CARD = re.compile(r"\b\d{4}[ -]?\d{4}[ -]?\d{4}[ -]?\d{4}\b")
AADHAAR = re.compile(r"\b\d{4}[ -]\d{4}[ -]\d{4}\b")
PAN = re.compile(r"\b[A-Z]{5}\d{4}[A-Z]\b")
SECRET = re.compile(r"\b(otp|password|passcode|pin|cvv)\b(\s*(?:is|:|-)\s*)\S+", re.I)


def normalize(text: str) -> str:
    """Unicode-normalize and drop invisible characters used to hide instructions."""
    return INVISIBLE_CHARS.sub("", unicodedata.normalize("NFKC", text))


class _VisibleText(HTMLParser):
    """Collects only text a human would actually see."""

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.parts: list[str] = []
        self.stack: list[tuple[str, str]] = []  # (tag, "show" | "hidden" | "skip")
        self.hidden_found = False

    def handle_starttag(self, tag, attrs):
        if tag in BLOCK_TAGS:
            self.parts.append("\n")
        if tag in VOID_TAGS:
            return
        attrs = dict(attrs)
        if tag in SKIP_TAGS:
            mode = "skip"
        elif "hidden" in attrs or HIDDEN_STYLE.search(attrs.get("style") or ""):
            mode = "hidden"
        else:
            mode = "show"
        self.stack.append((tag, mode))

    def handle_endtag(self, tag):
        if any(open_tag == tag for open_tag, _ in self.stack):
            while self.stack and self.stack.pop()[0] != tag:
                pass
        if tag in BLOCK_TAGS:
            self.parts.append("\n")

    def handle_data(self, data):
        modes = {mode for _, mode in self.stack}
        if "skip" in modes:
            return
        if "hidden" in modes:
            self.hidden_found = self.hidden_found or bool(data.strip())
            return
        self.parts.append(data)


def html_to_text(html: str) -> tuple[str, bool]:
    """Return (visible text, whether hidden text was found)."""
    parser = _VisibleText()
    parser.feed(html)
    parser.close()
    text = re.sub(r"\n\s*\n+", "\n\n", "".join(parser.parts))
    return text.strip(), parser.hidden_found


def strip_quoted(text: str) -> str:
    """Keep only the newest message, so old thread content can't carry instructions."""
    newest = text
    for header in (QUOTE_HEADER, FORWARD_HEADER):
        match = header.search(newest)
        if match and newest[: match.start()].strip():
            newest = newest[: match.start()]
    return "\n".join(line for line in newest.splitlines() if not line.lstrip().startswith(">"))


def _keep_last4(match: re.Match) -> str:
    return "XXXX" + re.sub(r"\D", "", match.group())[-4:]


def redact(text: str) -> str:
    """Mask cards, Aadhaar, PAN, OTPs/passwords and long account numbers."""
    text = CARD.sub(_keep_last4, text)
    text = AADHAAR.sub(_keep_last4, text)
    text = PAN.sub("[PAN]", text)
    text = SECRET.sub(lambda m: f"{m.group(1)}{m.group(2)}[REDACTED]", text)
    return mask_digits(text)


def sanitize(email: Email, max_chars: int = 8000) -> tuple[Email, list[str]]:
    """Return a cleaned copy of the email plus flags describing what was removed."""
    flags = []
    body = email.body
    if HTML_HINT.search(body):
        body, hidden = html_to_text(body)
        if hidden:
            flags.append("hidden_text")
    if INVISIBLE_CHARS.search(body):
        flags.append("invisible_chars")
    body = redact(strip_quoted(normalize(body))).strip()
    if len(body) > max_chars:
        body = body[:max_chars]
        flags.append("truncated")
    subject = redact(normalize(email.subject))[:300]
    return email.model_copy(update={"body": body, "subject": subject}), flags


# ----------------------------------------------------------- pre-filters ----

AUTO_SUBJECT = re.compile(
    r"^(automatic reply|auto-?reply|out of (the )?office|undeliverable|delivery status notification"
    r"|mail delivery (failed|subsystem))",
    re.I,
)
RELEVANT_WORDS = re.compile(
    r"\b(statement|account|loan|disburs\w*|emi|invoice|payment|refund|complaint|kyc|pan|aadhaar|incident"
    r"|outage|down|error|transaction|bank|verify|password|credit|debit)\b",
    re.I,
)


def _lower_headers(email: Email) -> dict[str, str]:
    return {key.lower(): value.lower() for key, value in email.headers.items()}


def is_auto_or_self(email: Email, own_address: str) -> bool:
    """Auto-replies, bounces and our own mail must never get a reply (prevents mail loops)."""
    headers = _lower_headers(email)
    sender = email.from_addr.lower()
    return (
        headers.get("auto-submitted", "no") != "no"
        or "x-autoreply" in headers
        or "x-autorespond" in headers
        or headers.get("precedence") == "auto_reply"
        or sender.partition("@")[0] in ("mailer-daemon", "postmaster")
        or (bool(own_address) and sender == own_address.lower())
        or bool(AUTO_SUBJECT.search(email.subject))
    )


COURTESY = re.compile(
    r"\b(thanks?|thank\s*you|thankyou|thx|ty|cheers|(?:much\s+)?appreciated?|noted|got\s+it|received|ok(?:ay)?"
    r"|great|perfect|sounds\s+good|will\s+do)\b",
    re.I,
)
STILL_NEEDS_SOMETHING = re.compile(  # any of these means the "thanks" comes with a real message
    r"\?|\d|\b(but|however|though|still|not|no|never|please|pls|kindly|help|urgent\w*|issue|problem|error|fail\w*"
    r"|wrong|again|refund\w*|when|why|how|what|where|which|can|could|would|need|want|missing|complain\w*"
    r"|attach\w*|request\w*|send|share|confirm|update\s+me|query|question)\b",
    re.I,
)


def is_courtesy_reply(email: Email) -> bool:
    """A short "thank you" / "got it" with nothing else to act on: it is recorded but never answered,
    so a customer saying thanks doesn't get yet another reply. Deliberately strict - any question, number,
    attachment or hint of a problem means the email is handled normally."""
    text = email.body.strip()
    words = re.findall(r"[a-z']+", text.lower())
    return (
        not email.attachment_names
        and 0 < len(words) <= 25
        and bool(COURTESY.search(text))
        and not STILL_NEEDS_SOMETHING.search(text)
    )


def is_irrelevant(email: Email) -> bool:
    """Bulk mail with nothing business-related in it. Banks send statements as bulk mail too,
    so the bulk header alone is not enough."""
    headers = _lower_headers(email)
    bulk = "list-unsubscribe" in headers or headers.get("precedence") in ("bulk", "list", "junk")
    return bulk and not RELEVANT_WORDS.search(f"{email.subject}\n{email.body}")


# ------------------------------------------- prompt injection / spoofing ----

STRONG_PATTERNS = {
    "ignore_instructions": r"\b(ignore|disregard|forget|override)\b.{0,40}?\b(previous|prior|above|earlier|all|your)\b"
    r".{0,20}?\b(instructions?|rules|prompts?|directions)\b",
    "role_marker": r"(^|\n)[\s>]*(system|assistant)\s*:|<\|im_(start|end)\|>|\[/?INST\]|<<SYS>>",
    "tool_name": r"\b(submit_triage|submit_reply)\b",
    "mass_forward": r"\b(forward|send|share)\b.{0,30}\b(all|every|these|this)\b.{0,10}"
    r"\b(emails?|messages?|mailbox|conversations?)\b",
    "bank_detail_change": r"\b(change|update|modify|replace)\b.{0,30}"
    r"\b(beneficiary|bank account|account details|account number|ifsc)\b",
}
WEAK_PATTERNS = {
    "act_as": r"\bact as\b|\bpretend (to be|you are)\b",
    "system_prompt": r"\bsystem prompt\b|\byour (instructions|prompt)\b",
    "encoded_blob": r"[A-Za-z0-9+/=]{200,}",
}
FREE_MAIL = {
    "gmail.com",
    "googlemail.com",
    "yahoo.com",
    "outlook.com",
    "hotmail.com",
    "live.com",
    "proton.me",
    "protonmail.com",
    "aol.com",
    "rediffmail.com",
    "icloud.com",
}
BRAND_WORDS = re.compile(r"\b(bank|hdfc|icici|sbi|axis|kotak|ltfs|l&t|finance|support|security|rbi|paypal)\b", re.I)
AUTH_FAIL = re.compile(r"\b(spf|dkim|dmarc)=(fail|softfail)\b", re.I)


def detect_injection(text: str) -> tuple[list[str], list[str]]:
    """Return (strong hits, weak hits). Strong = never let an LLM read it."""
    strong = [name for name, rx in STRONG_PATTERNS.items() if re.search(rx, text, re.I)]
    weak = [name for name, rx in WEAK_PATTERNS.items() if re.search(rx, text, re.I)]
    return strong, weak


def sender_signals(email: Email) -> list[str]:
    signals = []
    if AUTH_FAIL.search(email.headers.get("Authentication-Results", "")):
        signals.append("auth_failed")
    if email.sender_domain in FREE_MAIL and BRAND_WORDS.search(email.from_name):
        signals.append("brand_on_free_mail")
    reply_domain = email.reply_to.rpartition("@")[2].lower()
    if reply_domain and reply_domain != email.sender_domain:
        signals.append("reply_to_mismatch")
    return signals


class Inspection(BaseModel):
    block: bool = False  # quarantine without calling the LLM
    review: bool = False  # LLM may classify, but no automatic reply
    flags: list[str] = []
    sender_signals: list[str] = []


def inspect(raw: Email, clean: Email, sanitize_flags: list[str]) -> Inspection:
    """Scan the *raw* email (hidden text and quoted history included) for attacks."""
    raw_text = normalize(f"{raw.subject}\n{re.sub(r'<[^>]+>', ' ', raw.body)}")
    strong, weak = detect_injection(f"{raw_text}\n{clean.body}")
    senders = sender_signals(raw)
    return Inspection(
        block=bool(strong) or len(senders) >= 2,
        review=bool(weak) or "hidden_text" in sanitize_flags,
        flags=sanitize_flags + [f"injection:{name}" for name in strong + weak],
        sender_signals=senders,
    )


# --------------------------------------------------------------- output ----

URL = re.compile(r"https?://|www\.|\b[\w-]+\.(com|in|net|org|io|co|ru|xyz)\b", re.I)
EMAIL_ADDR = re.compile(r"[\w.+-]+@[\w-]+\.[\w.-]+")
PHONE = re.compile(r"(?<!\w)\+?\d(?:[\s-]?\d){9,}(?!\w)")
NUMBER = re.compile(r"\d[\d,]*")
COMMITMENT = re.compile(
    r"\bwe(?: will|'ll| shall| have)\s+(?:\w+\s+){0,2}?(transfer|refund|credit|disburse|release|update|change|waive)"
    r"|\b(refund|transfer)\s+(has been|is)\s+(processed|initiated|done|completed)"
    r"|\baccount\s+(has been\s+|is\s+)?(updated|changed)",
    re.I,
)
STATUS_CLAIM = re.compile(
    r"\b(?:is|are|was|were|is being|are being|has been|have been|will be|shall be)\s+"
    r"(processed|released|disbursed|credited|transferred|refunded|approved|paid|cleared|sanctioned)\b",
    re.I,
)
ACTION_CLAIM = re.compile(  # actions the agent never takes: it only drafts (or sends) one reply
    r"\b(?:has|have|had)\s+been\s+(sent|forwarded|passed|escalated|assigned|routed|shared|referred|handed"
    r"|notified|informed|alerted)\b"
    r"|\bwe(?:'ve|\s+have)\s+(?:already\s+)?(sent|forwarded|passed|escalated|assigned|routed|shared|referred"
    r"|notified|informed|alerted|contacted)\b",
    re.I,
)
AMOUNT = re.compile(r"(₹|\brs\.?|\binr|\busd|\$)\s?\d|\d[\d,]*(\.\d+)?\s?(rs|inr|rupees|lakh|crore)\b", re.I)
FINANCIAL = {Category.FINANCIAL_DISBURSEMENT, Category.PAYMENT_REQUEST, Category.BANK_STATEMENT}


def _numbers(text: str) -> set[str]:
    return {n.replace(",", "") for n in NUMBER.findall(text) if len(n.replace(",", "")) >= 2}


def check_reply(reply: str, source: str, canary: str, escalated: bool = False) -> list[str]:
    """Problems with an LLM-written reply. Empty list = safe to save as a draft. `escalated`: the email
    really is escalated (incident), so saying so is true."""
    problems = []
    if canary in reply:
        problems.append("canary_leak")
    if URL.search(reply):
        problems.append("url_in_reply")
    if EMAIL_ADDR.search(reply):
        problems.append("email_in_reply")
    if PHONE.search(reply):
        problems.append("phone_in_reply")
    if _numbers(reply) - _numbers(source):
        problems.append("ungrounded_number")
    if COMMITMENT.search(reply):
        problems.append("commitment")
    if _unverified_status_claims(reply, source):
        problems.append("unverified_status_claim")
    claims = {(match.group(1) or match.group(2)).lower() for match in ACTION_CLAIM.finditer(reply)}
    if claims - ({"escalated"} if escalated else set()):
        problems.append("unverified_action_claim")
    return problems


def _unverified_status_claims(reply: str, source: str) -> list[str]:
    """Payment statuses ("is being processed", "will be credited") the email itself never states.
    Repeating the email ("has been disbursed" when it says so) is fine."""
    return [
        match.group() for match in STATUS_CLAIM.finditer(reply) if not re.search(rf"\b{match.group(1)}\b", source, re.I)
    ]


def _signals_agree(category: Category, text: str) -> bool:
    if category in (Category.FINANCIAL_DISBURSEMENT, Category.PAYMENT_REQUEST):
        return bool(AMOUNT.search(text))
    if category is Category.BANK_STATEMENT:
        return "statement" in text.lower()
    return True


def check_result(
    clean: Email, result: EmailResult, inspection: Inspection, canary: str, min_confidence: float = 0.6
) -> EmailResult:
    """Final authority: downgrade anything doubtful to needs_review and drop its draft."""
    flags = list(dict.fromkeys(result.guardrail_flags + inspection.flags + inspection.sender_signals))
    triage = result.triage
    if triage is None:
        return result.model_copy(update={"guardrail_flags": flags})
    if triage.category is Category.PHISHING_SUSPICIOUS:
        return EmailResult(email_id=result.email_id, status=Status.QUARANTINED, triage=triage, guardrail_flags=flags)

    source = f"{clean.subject}\n{clean.body}"
    problems = []
    if canary in triage.summary:
        problems.append("canary_leak")
    if triage.confidence < min_confidence:
        problems.append("low_confidence")
    if inspection.review:
        problems.append("weak_injection_signal")
    if inspection.sender_signals and triage.category in FINANCIAL:
        problems.append("suspicious_sender")
    if not _signals_agree(triage.category, source):
        problems.append("category_signal_mismatch")
    if result.draft:
        problems += check_reply(
            f"{result.draft.subject}\n{result.draft.body}", source, canary, escalated=result.status is Status.ESCALATED
        )

    if problems:
        return EmailResult(
            email_id=result.email_id, status=Status.NEEDS_REVIEW, triage=triage, guardrail_flags=flags + problems
        )
    return result.model_copy(update={"guardrail_flags": flags})
