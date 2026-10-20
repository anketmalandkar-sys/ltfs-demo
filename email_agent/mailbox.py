"""Where emails come from and where drafts go. Mock (JSON file) and any IMAP server share one interface:
connect(), fetch_new(limit, skip), save_draft(email, draft), apply_label(email, label), close().
ImapWatcher waits for new mail (IMAP IDLE), so --watch reacts within seconds instead of polling.
Nothing here can send an email."""

import imaplib
import json
import logging
import re
import ssl
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import date, timedelta
from email import message_from_bytes, policy
from email.message import EmailMessage
from email.utils import parseaddr
from pathlib import Path

from .models import Email, ReplyDraft

KEPT_HEADERS = (
    "Authentication-Results",
    "Auto-Submitted",
    "Precedence",
    "List-Unsubscribe",
    "X-Autoreply",
    "X-Autorespond",
)
GMAIL_EXTENSION = "X-GM-EXT-1"  # Gmail's thread ids and labels
IDLE_LIMIT = 25 * 60  # servers may drop an IDLE after 29 minutes (RFC 2177)
IDLE_TAG = b"W1"
log = logging.getLogger("email_agent")


def _part_text(part) -> str:
    try:
        return part.get_content()
    except (LookupError, UnicodeDecodeError):  # unknown or lying charset
        return (part.get_payload(decode=True) or b"").decode("utf-8", errors="replace")


def parse_message(uid: str, raw: bytes, thread_id: str = "") -> Email:
    """Turn raw RFC 822 bytes into an Email. HTML is preferred over plain text so the
    guardrails can see (and strip) hidden HTML content."""
    msg = message_from_bytes(raw, policy=policy.default)
    from_name, from_addr = parseaddr(str(msg.get("From", "")))
    body_part = msg.get_body(preferencelist=("html", "plain"))
    message_id = str(msg.get("Message-ID", "")).strip()
    if not thread_id:  # no server thread id: name the thread after its first message
        earlier = str(msg.get("References", "")).split() or str(msg.get("In-Reply-To", "")).split()
        thread_id = earlier[0] if earlier else message_id
    return Email(
        id=uid,
        message_id=message_id,
        thread_id=thread_id,
        from_name=from_name,
        from_addr=from_addr,
        reply_to=parseaddr(str(msg.get("Reply-To", "")))[1],
        subject=str(msg.get("Subject", "")),
        date=str(msg.get("Date", "")),
        body=_part_text(body_part) if body_part else "",
        headers={name: str(msg[name]) for name in KEPT_HEADERS if msg[name] is not None},
        attachment_names=[part.get_filename() or "unnamed" for part in msg.iter_attachments()],
    )


def build_reply(original: Email, draft: ReplyDraft, sender: str) -> EmailMessage:
    """Reply goes to the From address only - a different Reply-To is a classic phishing trick."""
    msg = EmailMessage()
    msg["From"] = sender
    msg["To"] = original.from_addr
    msg["Subject"] = draft.subject
    if original.message_id:
        msg["In-Reply-To"] = original.message_id
        msg["References"] = original.message_id
    msg.set_content(draft.body)
    return msg


class MockMailbox:
    """Reads emails from a JSON file and writes drafts as .eml files. Used for demos and tests."""

    def __init__(self, inbox_path: Path, drafts_dir: Path, sender: str = "triage@ltfs-demo.local"):
        self.inbox_path = Path(inbox_path)
        self.drafts_dir = Path(drafts_dir)
        self.sender = sender
        self.labels: dict[str, list[str]] = {}
        self.expected: dict[str, dict] = {}

    def connect(self) -> None:
        items = json.loads(self.inbox_path.read_text(encoding="utf-8"))
        self.emails = [Email(**item) for item in items]
        self.expected = {item["id"]: item for item in items}

    def fetch_new(self, limit: int, skip: Callable[[Email], bool] | None = None) -> list[Email]:
        return [email for email in self.emails if not (skip and skip(email))][:limit]

    def save_draft(self, email: Email, draft: ReplyDraft) -> bool:
        """Returns False if a draft for this email already exists (it is left untouched)."""
        path = self.drafts_dir / f"{email.id}.eml"
        if path.exists():
            return False
        self.drafts_dir.mkdir(parents=True, exist_ok=True)
        path.write_bytes(build_reply(email, draft, self.sender).as_bytes())
        return True

    def apply_label(self, email: Email, label: str) -> None:
        self.labels.setdefault(email.id, []).append(label)

    def close(self) -> None:
        pass


@dataclass
class ImapAccount:
    """How to reach one IMAP mailbox. Always TLS with a verified certificate - imaplib's own
    default does not verify it."""

    host: str
    username: str
    secret: str = field(repr=False)  # password / app password, or an OAuth2 access token
    auth: str = "password"  # password | xoauth2
    port: int = 993
    inbox: str = "INBOX"
    imap_factory: Callable = field(default=imaplib.IMAP4_SSL, repr=False)

    def open(self) -> tuple[imaplib.IMAP4, set[str]]:
        """Connect and log in; returns the connection and the server's capabilities.
        Raises imaplib.IMAP4.error on bad credentials - no silent loop."""
        conn = self.imap_factory(self.host, self.port, ssl_context=ssl.create_default_context())
        if self.auth == "xoauth2":
            sasl = f"user={self.username}\x01auth=Bearer {self.secret}\x01\x01".encode()
            conn.authenticate("XOAUTH2", lambda _: sasl)
        else:
            conn.login(self.username, self.secret)
        _, data = conn.capability()  # asked after login: some servers only list extensions then
        return conn, set(data[0].decode().upper().split())


class ImapMailbox:
    """Any IMAP server. Gmail's extensions are used when the server offers them, plain IMAP
    otherwise. The inbox is opened read-only unless labels are enabled, and messages are fetched
    with BODY.PEEK so they stay unread."""

    def __init__(
        self, account: ImapAccount, sender: str, days: int = 7, apply_labels: bool = False, drafts_folder: str = ""
    ):
        self.account = account
        self.sender = sender
        self.days = days
        self.apply_labels = apply_labels
        self._drafts_folder = drafts_folder or None

    def connect(self) -> None:
        self.conn, caps = self.account.open()
        self.gmail = GMAIL_EXTENSION in caps
        self._select_inbox()

    def _select_inbox(self) -> None:
        self.conn.select(self.account.inbox, readonly=not self.apply_labels)

    def fetch_new(self, limit: int, skip: Callable[[Email], bool] | None = None) -> list[Email]:
        """Newest first. Skipped emails (already handled, but still unread) don't count toward the limit."""
        since = (date.today() - timedelta(days=self.days)).strftime("%d-%b-%Y")
        _, data = self.conn.uid("SEARCH", None, "UNSEEN", "SINCE", since)
        emails = []
        for uid in reversed(data[0].split()):
            if len(emails) == limit:
                break
            email = self._fetch(uid.decode())
            if not (skip and skip(email)):
                emails.append(email)
        return emails

    def _fetch(self, uid: str) -> Email:
        items = "(X-GM-THRID BODY.PEEK[])" if self.gmail else "(BODY.PEEK[])"
        _, data = self.conn.uid("FETCH", uid, items)
        meta, raw = next(item for item in data if isinstance(item, tuple))
        thread = re.search(rb"X-GM-THRID (\d+)", meta)
        return parse_message(uid, raw, thread.group(1).decode() if thread else "")

    def _drafts(self) -> str:
        """The Drafts folder name depends on provider and language, so find it by its \\Drafts flag."""
        if self._drafts_folder is None:
            _, lines = self.conn.list()
            for line in lines:
                match = re.match(rb'\(([^)]*)\) "[^"]*" (.+)$', line)
                if match and b"\\Drafts" in match.group(1):
                    self._drafts_folder = match.group(2).decode()
                    break
            else:
                raise RuntimeError("Could not find the Drafts folder; set IMAP_DRAFTS_FOLDER")
        return self._drafts_folder

    def save_draft(self, email: Email, draft: ReplyDraft) -> bool:
        """Returns False if a draft replying to this email already exists (it is left untouched)."""
        folder = self._drafts()
        if email.message_id and self._draft_exists(folder, email.message_id):
            return False
        message = build_reply(email, draft, self.sender).as_bytes()
        self.conn.append(folder, "(\\Draft)", imaplib.Time2Internaldate(time.time()), message)
        return True

    def _draft_exists(self, folder: str, message_id: str) -> bool:
        self.conn.select(folder, readonly=True)
        try:
            _, data = self.conn.uid("SEARCH", None, "HEADER", "In-Reply-To", message_id)
        finally:  # go back to the inbox so UIDs and labels keep referring to it
            self._select_inbox()
        return bool(data and data[0].split())

    def apply_label(self, email: Email, label: str) -> None:
        """A Gmail label, or an IMAP keyword on other servers (not every server allows those)."""
        if not self.apply_labels:
            raise PermissionError("Inbox opened read-only; run with --apply-labels to label emails")
        if self.gmail:
            self.conn.uid("STORE", email.id, "+X-GM-LABELS", f'("{label}")')
            return
        typ, _ = self.conn.uid("STORE", email.id, "+FLAGS", f"({label})")
        if typ != "OK":
            raise RuntimeError("This server does not accept custom keywords; run without --apply-labels")

    def close(self) -> None:
        self.conn.logout()


class ImapWatcher:
    """Waits for new mail with IMAP IDLE on its own connection, so the agent's connection is only
    opened when there is something to do. Servers without IDLE are simply polled."""

    def __init__(self, account: ImapAccount, sleep: Callable[[float], None] = time.sleep):
        self.account = account
        self.sleep = sleep
        self.uidnext: int | None = None  # the inbox's next UID when we last woke up

    def wait(self, timeout: float) -> bool:
        """True when new mail arrived; False after `timeout` seconds without any, or when the
        connection dropped. Bad credentials raise imaplib.IMAP4.error."""
        conn, caps = self.account.open()
        if "IDLE" not in caps:
            conn.logout()
            self.sleep(timeout)
            return False
        usable = True
        try:
            if self._new_since_last_wake(conn):  # arrived while the agent was busy
                return True
            _, data = conn.select(self.account.inbox, readonly=True)
            self._idle(conn, min(timeout, IDLE_LIMIT), count=int(data[0]))
            self._new_since_last_wake(conn)  # remember where the coming run starts
            return True
        except TimeoutError:
            usable = False
            return False
        except (OSError, imaplib.IMAP4.abort) as exc:
            usable = False
            log.warning("IMAP connection dropped while waiting (%s); checking now", type(exc).__name__)
            return False
        finally:
            _close(conn, usable)

    def _new_since_last_wake(self, conn) -> bool:
        _, data = conn.status(self.account.inbox, "(UIDNEXT)")
        uidnext = int(re.search(rb"UIDNEXT (\d+)", data[0]).group(1))
        changed = self.uidnext is not None and uidnext != self.uidnext
        self.uidnext = uidnext
        return changed

    @staticmethod
    def _idle(conn, timeout: float, count: int) -> None:
        """RFC 2177 IDLE by hand (Python 3.12's imaplib has none). Returns once the server reports
        a new message; raises TimeoutError if none comes within `timeout` seconds. `count` is the
        inbox size: removing mail also sends EXISTS (a lower one), which is not new mail."""
        conn.send(IDLE_TAG + b" IDLE\r\n")
        if not _readline(conn).startswith(b"+"):
            raise imaplib.IMAP4.abort("server refused IDLE")
        deadline = time.monotonic() + timeout
        while True:  # a deadline, not just a socket timeout: some servers send keep-alive lines
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise TimeoutError
            conn.sock.settimeout(remaining)
            update = re.match(rb"\* (\d+) (EXISTS|EXPUNGE)$", _readline(conn).rstrip())
            if not update:
                continue
            if update.group(2) == b"EXPUNGE":
                count -= 1
            elif int(update.group(1)) > count:
                break
            else:
                count = int(update.group(1))
        conn.sock.settimeout(None)
        conn.send(b"DONE\r\n")
        while not _readline(conn).startswith(IDLE_TAG + b" "):
            pass


def _readline(conn) -> bytes:
    line = conn.readline()
    if not line:
        raise imaplib.IMAP4.abort("connection closed by server")
    return line


def _close(conn, usable: bool) -> None:
    """Log out politely, or just drop a connection that timed out or broke."""
    try:
        conn.logout() if usable else conn.shutdown()
    except (OSError, imaplib.IMAP4.error):
        pass
