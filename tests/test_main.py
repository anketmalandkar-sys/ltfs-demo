import imaplib
import logging

import pytest

from email_agent import main as cli
from email_agent.llm import GeminiClient, HuggingFaceClient

from .test_agent import FakeLLM, oracle


@pytest.fixture
def env(monkeypatch, tmp_path):
    for name in (
        "LLM_PROVIDER",
        "GEMINI_API_KEY",
        "HF_TOKEN",
        "IMAP_HOST",
        "IMAP_USERNAME",
        "IMAP_PASSWORD",
        "IMAP_AUTH",
        "IMAP_OAUTH_TOKEN",
        "MAIL_ADDRESS",
        "AUTO_SEND",
        "AUTO_SEND_CATEGORIES",
        "SMTP_HOST",
        "SMTP_PORT",
        "MAIL_PROFILE",
        "APPLY_LABELS",
    ):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("OUTPUT_DIR", str(tmp_path))
    monkeypatch.chdir(tmp_path.parent)  # no stray .env file
    monkeypatch.setattr(cli, "ENV_FILE", None)
    monkeypatch.setattr(cli, "SECRETS_DIR", str(tmp_path / "no-secrets"))
    return monkeypatch


def test_mock_run_prints_table_and_accuracy(env, capsys, tmp_path):
    env.setattr(cli, "build_llm", lambda settings: FakeLLM(oracle))
    env.setenv("MOCK_INBOX", str(cli.ROOT / "data" / "mock_inbox.json"))
    assert cli.main(["--source", "mock", "--limit", "50"]) == 0
    out = capsys.readouterr().out
    assert "m08" in out and "drafted" in out and "customer_complaint" in out
    assert "Accuracy vs expected status: 23/23" in out
    assert (tmp_path / "results.jsonl").exists()


def test_mock_rerun_only_picks_up_unfinished_emails_unless_fresh(env, capsys):
    env.setattr(cli, "build_llm", lambda settings: FakeLLM(oracle))
    env.setenv("MOCK_INBOX", str(cli.ROOT / "data" / "mock_inbox.json"))
    cli.main(["--source", "mock", "--limit", "50"])
    capsys.readouterr()
    cli.main(["--source", "mock", "--limit", "50"])
    assert "0 emails processed" in capsys.readouterr().out
    cli.main(["--source", "mock", "--limit", "50", "--fresh"])
    assert "23 emails processed" in capsys.readouterr().out


def test_only_flag_reruns_specific_emails_beyond_default_limit(env, capsys):
    env.setattr(cli, "build_llm", lambda settings: FakeLLM(oracle))
    env.setenv("MOCK_INBOX", str(cli.ROOT / "data" / "mock_inbox.json"))
    cli.main(["--source", "mock", "--limit", "50"])
    capsys.readouterr()
    cli.main(["--source", "mock", "--only", "m06", "m20"])  # m20 is past the default --limit of 10
    out = capsys.readouterr().out
    assert "2 emails processed" in out
    assert "m06" in out and "m20" in out and "m07" not in out


def test_only_with_unknown_id_says_so(env, capsys):
    env.setattr(cli, "build_llm", lambda settings: FakeLLM(oracle))
    env.setenv("MOCK_INBOX", str(cli.ROOT / "data" / "mock_inbox.json"))
    cli.main(["--source", "mock", "--only", "m99"])
    assert "Not found among fetched emails: m99" in capsys.readouterr().out


def test_stopped_run_explains_why(env, capsys):
    from email_agent.llm import FatalLLMError

    def broken(email_id, step):
        raise FatalLLMError("gemini returned HTTP 404: model is no longer available")

    env.setattr(cli, "build_llm", lambda settings: FakeLLM(broken))
    env.setenv("MOCK_INBOX", str(cli.ROOT / "data" / "mock_inbox.json"))
    assert cli.main(["--source", "mock"]) == 0
    out = capsys.readouterr().out
    assert "Run stopped early: gemini returned HTTP 404: model is no longer available" in out


def test_missing_api_key_is_a_clear_error(env):
    from email_agent.config import Settings

    with pytest.raises(SystemExit, match="GEMINI_API_KEY"):
        cli.build_llm(Settings(_env_file=None))
    with pytest.raises(SystemExit, match="HF_TOKEN"):
        cli.build_llm(Settings(_env_file=None, llm_provider="huggingface"))


def test_build_llm_picks_provider(env):
    from email_agent.config import Settings

    assert isinstance(cli.build_llm(Settings(_env_file=None, gemini_api_key="k")), GeminiClient)
    hf = cli.build_llm(Settings(_env_file=None, llm_provider="huggingface", hf_token="t", hf_model="org/m"))
    assert isinstance(hf, HuggingFaceClient) and hf.model == "org/m"


def test_build_llm_applies_requests_per_minute(env):
    from email_agent.config import Settings

    client = cli.build_llm(Settings(_env_file=None, gemini_api_key="k", llm_requests_per_minute=5))
    assert client.min_interval == 12


def test_imap_needs_server_and_credentials(env):
    env.setattr(cli, "build_llm", lambda settings: FakeLLM(oracle))
    with pytest.raises(SystemExit, match="IMAP_HOST, IMAP_USERNAME, IMAP_PASSWORD"):
        cli.main(["--source", "imap"])
    env.setenv("IMAP_HOST", "imap.example.com")
    env.setenv("IMAP_USERNAME", "me@x.com")
    env.setenv("IMAP_AUTH", "xoauth2")
    with pytest.raises(SystemExit, match="IMAP_OAUTH_TOKEN"):
        cli.main(["--source", "imap"])


def test_profile_supplies_provider_settings(env):
    settings = cli.load_settings("gmail")
    assert (settings.imap_host, settings.imap_port, settings.imap_auth) == ("imap.gmail.com", 993, "password")
    assert cli.load_settings("outlook").imap_auth == "xoauth2"
    with pytest.raises(SystemExit, match="nope"):
        cli.load_settings("nope")


def test_secrets_are_read_from_files(env, tmp_path):
    secrets = tmp_path / "secrets"
    secrets.mkdir()
    (secrets / "imap_password").write_text("app-pass\n")  # trailing newline, as `echo` writes it
    (secrets / "hf_token").write_text("")  # an empty file means "not used"
    env.setattr(cli, "SECRETS_DIR", str(secrets))
    settings = cli.load_settings(None)
    assert settings.imap_password.get_secret_value() == "app-pass"
    assert not settings.hf_token


def test_secret_in_environment_is_warned_about_when_secret_files_are_used(env, tmp_path, caplog):
    (tmp_path / "secrets").mkdir()
    env.setattr(cli, "SECRETS_DIR", str(tmp_path / "secrets"))
    env.setenv("IMAP_PASSWORD", "leaky")
    with caplog.at_level(logging.WARNING):
        cli.load_settings(None)
    assert "IMAP_PASSWORD" in caplog.text and "leaky" not in caplog.text


class FakeWatcher:
    def __init__(self, results):
        self.results = list(results)
        self.timeouts = []
        self.account = None

    def wait(self, timeout):
        self.timeouts.append(timeout)
        result = self.results.pop(0)
        if isinstance(result, BaseException):
            raise result
        return result


def watch_imap(env, watcher):
    for name, value in (
        ("IMAP_HOST", "imap.example.com"),
        ("IMAP_USERNAME", "me@x.com"),
        ("IMAP_PASSWORD", "app-pass"),
    ):
        env.setenv(name, value)
    runs = []
    env.setattr(cli, "run_once", lambda settings, args: runs.append(args.source))
    env.setattr(cli, "build_watcher", lambda settings: watcher)
    env.setattr(cli.time, "sleep", lambda seconds: pytest.fail("IMAP watch must not sleep"))
    return runs


def test_imap_watch_waits_for_new_mail_instead_of_sleeping(env):
    watcher = FakeWatcher([True, False, KeyboardInterrupt()])
    runs = watch_imap(env, watcher)
    assert cli.main(["--source", "imap", "--watch", "--interval", "60"]) == 0
    assert runs == ["imap"] * 3
    assert watcher.timeouts == [60, 60, 60]
    assert watcher.account.host == "imap.example.com"  # refreshed from settings each cycle


def test_imap_watch_stops_on_login_failure(env):
    runs = watch_imap(env, FakeWatcher([imaplib.IMAP4.error("[AUTHENTICATIONFAILED]")]))
    with pytest.raises(SystemExit, match="IMAP login or command failed"):
        cli.main(["--source", "imap", "--watch"])
    assert runs == ["imap"]


def test_mock_watch_sleeps(env):
    slept = []

    def sleep(seconds):
        slept.append(seconds)
        raise KeyboardInterrupt

    env.setattr(cli, "run_once", lambda settings, args: None)
    env.setattr(cli.time, "sleep", sleep)
    assert cli.main(["--source", "mock", "--watch", "--interval", "7"]) == 0
    assert slept == [7]


def test_sending_is_on_by_default_and_can_be_turned_off(env):
    from email_agent.config import Settings
    from email_agent.sender import MockSender

    assert isinstance(cli.build_sender(Settings(_env_file=None), "mock"), MockSender)
    assert cli.build_sender(Settings(_env_file=None, auto_send=False), "mock") is None


def test_send_categories_are_validated(env):
    from email_agent.config import Settings
    from email_agent.models import Category

    settings = Settings(_env_file=None, auto_send_categories=" incident, customer_complaint ")
    assert settings.send_categories == {Category.INCIDENT, Category.CUSTOMER_COMPLAINT}
    for bad in ("incidnet", "phishing_suspicious"):
        with pytest.raises(ValueError, match=bad):
            Settings(_env_file=None, auto_send_categories=bad)


def test_imap_sending_without_smtp_host_keeps_drafts(env, caplog):
    from email_agent.config import Settings
    from email_agent.sender import SmtpSender

    creds = dict(_env_file=None, imap_username="me@x.com", imap_password="p")
    with caplog.at_level(logging.WARNING):
        assert cli.build_sender(Settings(**creds), "imap") is None
    assert "SMTP_HOST" in caplog.text
    caplog.clear()
    with caplog.at_level(logging.WARNING):
        sender = cli.build_sender(Settings(**creds, smtp_host="smtp.x.com"), "imap")
    assert isinstance(sender, SmtpSender) and sender.address == "me@x.com" and sender.port == 465
    assert "AUTO_SEND is on" in caplog.text and "customer_complaint" in caplog.text


def test_mock_run_sends_and_saves_drafts_by_default(env, tmp_path, capsys):
    env.setattr(cli, "build_llm", lambda settings: FakeLLM(oracle))
    env.setenv("MOCK_INBOX", str(cli.ROOT / "data" / "mock_inbox.json"))
    cli.main(["--limit", "50"])
    sent = {p.stem for p in (tmp_path / "sent").glob("*.eml")}
    drafts = {p.stem for p in (tmp_path / "drafts").glob("*.eml")}
    assert sent and drafts and not sent & drafts
    assert "Accuracy vs expected status: 23/23" in capsys.readouterr().out


def test_sending_and_drafts_can_be_turned_off(env, tmp_path):
    env.setattr(cli, "build_llm", lambda settings: FakeLLM(oracle))
    env.setenv("MOCK_INBOX", str(cli.ROOT / "data" / "mock_inbox.json"))
    env.setenv("AUTO_SEND", "false")
    cli.main(["--limit", "50", "--no-save-drafts"])
    assert not (tmp_path / "sent").exists() and not list((tmp_path / "drafts").glob("*.eml"))


def test_source_is_mock_without_a_mailbox_and_imap_with_one(env):
    runs = []
    env.setattr(cli, "run_once", lambda settings, args: runs.append(args.source))
    cli.main([])
    env.setenv("IMAP_USERNAME", "me@x.com")
    cli.main([])
    cli.main(["--source", "mock"])
    assert runs == ["mock", "imap", "mock"]


def test_labels_are_on_by_default_except_where_the_profile_turns_them_off(env):
    seen = []
    env.setattr(cli, "run_once", lambda settings, args: seen.append(args.apply_labels))
    cli.main([])
    cli.main(["--no-apply-labels"])
    cli.main(["--profile", "outlook"])
    cli.main(["--profile", "outlook", "--apply-labels"])
    assert seen == [True, False, False, True]


def test_mail_profile_setting_picks_the_profile(env, tmp_path):
    env.setenv("MAIL_PROFILE", "yahoo")
    assert cli.load_settings(None).imap_host == "imap.mail.yahoo.com"
    assert cli.load_settings("gmail").imap_host == "imap.gmail.com"  # --profile wins
    env.delenv("MAIL_PROFILE")
    env_file = tmp_path / "local.env"
    env_file.write_text("MAIL_PROFILE=outlook\n")
    env.setattr(cli, "ENV_FILE", str(env_file))
    assert cli.load_settings(None).imap_auth == "xoauth2"


def test_gmail_profile_has_smtp(env):
    settings = cli.load_settings("gmail")
    assert (settings.smtp_host, settings.smtp_port) == ("smtp.gmail.com", 465)


def test_wakeups_are_counted(env, recorded):
    watch_imap(env, FakeWatcher([True, False, KeyboardInterrupt()]))
    cli.main(["--source", "imap", "--watch"])
    assert recorded("mail_wakeups", reason="new_mail") == 1 and recorded("mail_wakeups", reason="timeout") == 1


def test_telemetry_setup_and_run_summary(env, caplog, recorded):
    env.setattr(cli, "build_llm", lambda settings: FakeLLM(oracle))
    env.setenv("MOCK_INBOX", str(cli.ROOT / "data" / "mock_inbox.json"))
    exporters = []
    env.setattr(cli, "setup_telemetry", lambda exporter: exporters.append(exporter))
    with caplog.at_level("INFO", logger="email_agent"):
        cli.main(["--source", "mock", "--limit", "2"])
    assert exporters == ["none"]
    summary = next(r for r in caplog.records if getattr(r, "event", None) == "run.finished")
    assert summary.__dict__["run.emails_processed"] == 2
    assert recorded("run_duration") == 1


def test_build_llm_applies_timeout(env):
    from email_agent.config import Settings

    assert cli.build_llm(Settings(_env_file=None, gemini_api_key="k")).timeout == 90
    assert cli.build_llm(Settings(_env_file=None, gemini_api_key="k", llm_timeout_seconds=40)).timeout == 40


def test_run_summary_has_tokens_and_cost(env, caplog):
    env.setattr(cli, "build_llm", lambda settings: FakeLLM(oracle))
    env.setenv("MOCK_INBOX", str(cli.ROOT / "data" / "mock_inbox.json"))
    with caplog.at_level("INFO", logger="email_agent"):
        cli.main(["--source", "mock", "--limit", "2"])
    summary = next(r for r in caplog.records if getattr(r, "event", None) == "run.finished").__dict__
    assert {"run.input_tokens", "run.output_tokens", "run.cost_usd"} <= set(summary)
