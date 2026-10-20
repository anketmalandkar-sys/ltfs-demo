import ssl
from email import message_from_bytes

from email_agent.models import Email, ReplyDraft
from email_agent.sender import MockSender, SmtpSender

ORIGINAL = Email(id="7", message_id="<m7@x>", from_addr="cust@x.com", reply_to="evil@y.com", subject="EMI")
DRAFT = ReplyDraft(subject="Re: EMI", body="We are on it.")


class FakeSMTP:
    instances = []

    def __init__(self, host, port, context=None, timeout=None):
        self.host, self.port, self.context = host, port, context
        self.calls = []
        FakeSMTP.instances.append(self)

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.calls.append(("quit",))

    def starttls(self, context=None):
        self.context = context
        self.calls.append(("starttls",))

    def login(self, user, password):
        self.calls.append(("login", user, password))

    def auth(self, mechanism, authobject, initial_response_ok=True):
        self.calls.append(("auth", mechanism, authobject()))

    def send_message(self, message):
        self.calls.append(("send", message))


def sender(**kw):
    FakeSMTP.instances = []
    return SmtpSender(
        **{
            "host": "smtp.example.com",
            "username": "me@x.com",
            "secret": "app-pass",
            "address": "me@x.com",
            "smtp_ssl": FakeSMTP,
            "smtp_plain": FakeSMTP,
            **kw,
        }
    )


def test_smtp_sends_threaded_reply_to_from_address_over_verified_tls():
    sender().send(ORIGINAL, DRAFT)
    smtp = FakeSMTP.instances[0]
    assert (smtp.host, smtp.port) == ("smtp.example.com", 465)
    assert smtp.context.verify_mode == ssl.CERT_REQUIRED and smtp.context.check_hostname
    assert ("login", "me@x.com", "app-pass") in smtp.calls
    message = next(c[1] for c in smtp.calls if c[0] == "send")
    assert message["To"] == "cust@x.com"  # never the Reply-To
    assert message["From"] == "me@x.com" and message["In-Reply-To"] == "<m7@x>"
    assert ("starttls",) not in smtp.calls


def test_port_587_upgrades_with_starttls_before_login():
    sender(port=587).send(ORIGINAL, DRAFT)
    calls = [c[0] for c in FakeSMTP.instances[0].calls]
    assert calls.index("starttls") < calls.index("login")
    assert FakeSMTP.instances[0].context.verify_mode == ssl.CERT_REQUIRED


def test_xoauth2_uses_bearer_token():
    sender(auth="xoauth2", secret="tok").send(ORIGINAL, DRAFT)
    assert ("auth", "XOAUTH2", "user=me@x.com\x01auth=Bearer tok\x01\x01") in FakeSMTP.instances[0].calls


def test_secret_not_in_repr():
    assert "app-pass" not in repr(sender())


def test_mock_sender_writes_eml(tmp_path):
    MockSender(tmp_path / "sent", address="triage@demo.local").send(ORIGINAL, DRAFT)
    saved = message_from_bytes((tmp_path / "sent" / "7.eml").read_bytes())
    assert saved["To"] == "cust@x.com" and saved["From"] == "triage@demo.local"
