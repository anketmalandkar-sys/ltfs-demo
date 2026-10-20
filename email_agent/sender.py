"""Sends approved replies. Only Actions calls this, and only when AUTO_SEND is on - see Actions for
the rules (allowed categories, no guardrail flags, per-run limit, never twice)."""

import smtplib
import ssl
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path

from .mailbox import build_reply
from .models import Email, ReplyDraft


@dataclass
class SmtpSender:
    """Any SMTP server: implicit TLS on 465, STARTTLS on other ports. The certificate is always
    verified, and the reply always goes to the original From address (build_reply)."""

    host: str
    username: str
    secret: str = field(repr=False)  # password / app password, or an OAuth2 access token
    address: str  # our From address
    auth: str = "password"  # password | xoauth2
    port: int = 465
    smtp_ssl: Callable = field(default=smtplib.SMTP_SSL, repr=False)
    smtp_plain: Callable = field(default=smtplib.SMTP, repr=False)

    def send(self, original: Email, draft: ReplyDraft) -> None:
        context = ssl.create_default_context()
        if self.port == 465:
            smtp = self.smtp_ssl(self.host, self.port, context=context, timeout=30)
        else:
            smtp = self.smtp_plain(self.host, self.port, timeout=30)
            smtp.starttls(context=context)
        with smtp:
            if self.auth == "xoauth2":
                sasl = f"user={self.username}\x01auth=Bearer {self.secret}\x01\x01"
                smtp.auth("XOAUTH2", lambda challenge=None: sasl, initial_response_ok=True)
            else:
                smtp.login(self.username, self.secret)
            smtp.send_message(build_reply(original, draft, self.address))


class MockSender:
    """Mock mode: 'sent' replies are written to output/sent/<id>.eml."""

    def __init__(self, sent_dir: Path, address: str):
        self.sent_dir = Path(sent_dir)
        self.address = address

    def send(self, original: Email, draft: ReplyDraft) -> None:
        self.sent_dir.mkdir(parents=True, exist_ok=True)
        (self.sent_dir / f"{original.id}.eml").write_bytes(build_reply(original, draft, self.address).as_bytes())
