"""Command line entry point.

python -m email_agent.main            # the mailbox from .env (MAIL_PROFILE, IMAP_USERNAME), or the mock inbox
python -m email_agent.main --watch    # keep running and handle new mail as it arrives

Drafts, labels and sending (AUTO_SEND) are on by default; see --help for the flags that turn them off.
"""

import argparse
import imaplib
import logging
import os
import sys
import time
from pathlib import Path

from dotenv import dotenv_values

from .actions import Actions
from .agent import Agent
from .config import Settings
from .llm import GeminiClient, HuggingFaceClient
from .mailbox import ImapAccount, ImapMailbox, ImapWatcher, MockMailbox
from .models import RunReport
from .sender import MockSender, SmtpSender
from .state import State
from .telemetry import event, mail_wakeups, prime_metrics, run_duration, setup_telemetry, tracer

ROOT = Path(__file__).resolve().parent.parent
ENV_FILE = ".env"
SECRETS_DIR = os.environ.get("SECRETS_DIR", "/run/secrets")  # Docker secrets: one file per secret
SECRET_FIELDS = ("imap_password", "imap_oauth_token", "gemini_api_key", "hf_token")
MOCK_ADDRESS = "triage@ltfs-demo.local"
log = logging.getLogger("email_agent")


def profile_name(profile: str | None) -> str | None:
    """--profile, else MAIL_PROFILE from the environment or .env."""
    if profile:
        return profile
    if os.environ.get("MAIL_PROFILE"):
        return os.environ["MAIL_PROFILE"]
    if ENV_FILE and Path(ENV_FILE).is_file():
        return dotenv_values(ENV_FILE).get("MAIL_PROFILE") or None
    return None


def load_settings(profile: str | None) -> Settings:
    """Environment > .env > config/<profile>.env > files in SECRETS_DIR."""
    env_files = []
    if profile := profile_name(profile):
        path = ROOT / "config" / f"{profile}.env"
        if not path.is_file():
            raise SystemExit(f"Unknown profile '{profile}': {path} not found")
        env_files.append(path)
    if ENV_FILE:
        env_files.append(ENV_FILE)
    secrets_dir = Path(SECRETS_DIR)
    if secrets_dir.is_dir():
        for name in SECRET_FIELDS:
            if os.environ.get(name.upper()):
                log.warning("%s came from the environment; put it in %s instead", name.upper(), secrets_dir)
    return Settings(_env_file=tuple(env_files) or None, _secrets_dir=secrets_dir if secrets_dir.is_dir() else None)


def build_llm(settings: Settings):
    rpm, timeout = settings.llm_requests_per_minute, settings.llm_timeout_seconds
    if settings.llm_provider == "gemini":
        if not settings.gemini_api_key:
            raise SystemExit("Set GEMINI_API_KEY (or LLM_PROVIDER=huggingface with HF_TOKEN)")
        return GeminiClient(
            settings.gemini_api_key.get_secret_value(), settings.gemini_model, requests_per_minute=rpm, timeout=timeout
        )
    if not settings.hf_token:
        raise SystemExit("Set HF_TOKEN for LLM_PROVIDER=huggingface")
    return HuggingFaceClient(
        settings.hf_token.get_secret_value(), settings.hf_model, requests_per_minute=rpm, timeout=timeout
    )


def mail_login(settings: Settings, host_name: str, host: str) -> str:
    """The login secret, shared by IMAP and SMTP. Exits with the names of whatever is missing."""
    oauth = settings.imap_auth == "xoauth2"
    secret = settings.imap_oauth_token if oauth else settings.imap_password
    missing = [
        name
        for name, value in (
            (host_name, host),
            ("IMAP_USERNAME", settings.imap_username),
            ("IMAP_OAUTH_TOKEN" if oauth else "IMAP_PASSWORD", secret),
        )
        if not value
    ]
    if missing:
        raise SystemExit(
            f"Set {', '.join(missing)} for the mailbox (MAIL_PROFILE=gmail|outlook|yahoo sets the servers; "
            f"secrets can also be files in {SECRETS_DIR})"
        )
    return secret.get_secret_value()


def build_account(settings: Settings) -> ImapAccount:
    return ImapAccount(
        host=settings.imap_host,
        port=settings.imap_port,
        username=settings.imap_username,
        secret=mail_login(settings, "IMAP_HOST", settings.imap_host),
        auth=settings.imap_auth,
        inbox=settings.imap_inbox,
    )


def build_sender(settings: Settings, source: str):
    """None if AUTO_SEND is off, or no SMTP server is known. SMTP logs in with the same credentials as IMAP."""
    if not settings.auto_send:
        return None
    if source == "imap" and not settings.smtp_host:
        log.warning("AUTO_SEND is on but SMTP_HOST is not set (use MAIL_PROFILE); replies are only saved as drafts")
        return None
    categories = ", ".join(sorted(c.value for c in settings.send_categories)) or "none"
    log.warning(
        "AUTO_SEND is on: clean replies for %s are sent (max %s per run)", categories, settings.max_sends_per_run
    )
    if source == "mock":
        return MockSender(settings.output_dir / "sent", address=MOCK_ADDRESS)
    return SmtpSender(
        host=settings.smtp_host,
        port=settings.smtp_port,
        username=settings.imap_username,
        secret=mail_login(settings, "SMTP_HOST", settings.smtp_host),
        auth=settings.imap_auth,
        address=own_address(settings, source),
    )


def own_address(settings: Settings, source: str) -> str:
    return (settings.mail_address or settings.imap_username) if source == "imap" else MOCK_ADDRESS


def build_mailbox(settings: Settings, source: str, apply_labels: bool):
    if source == "mock":
        return MockMailbox(settings.mock_inbox, settings.output_dir / "drafts", sender=MOCK_ADDRESS)
    return ImapMailbox(
        build_account(settings),
        sender=own_address(settings, source),
        days=settings.mail_days,
        apply_labels=apply_labels,
        drafts_folder=settings.imap_drafts_folder,
    )


def build_watcher(settings: Settings) -> ImapWatcher:
    return ImapWatcher(build_account(settings))


def print_report(report: RunReport, expected: dict) -> None:
    print(f"\n{'ID':<8} {'STATUS':<13} {'CATEGORY':<24} {'CONF':>5}  FLAGS")
    for r in report.results:
        category = r.triage.category.value if r.triage else "-"
        confidence = f"{r.triage.confidence:.2f}" if r.triage else "-"
        print(f"{r.email_id:<8} {r.status.value:<13} {category:<24} {confidence:>5}  {', '.join(r.guardrail_flags)}")
    print(f"\n{len(report.results)} emails processed with {report.provider}/{report.model}")
    if expected and report.results:
        hits = sum(expected.get(r.email_id, {}).get("expected_status") == r.status.value for r in report.results)
        print(f"Accuracy vs expected status: {hits}/{len(report.results)}")


def run_once(settings: Settings, args) -> RunReport:
    if args.fresh:  # forget what was processed, so every email is handled again
        (settings.output_dir / f"state-{args.source}.json").unlink(missing_ok=True)
    mailbox = build_mailbox(settings, args.source, args.apply_labels)
    agent = Agent(
        settings,
        mailbox,
        build_llm(settings),
        State(settings.output_dir / f"state-{args.source}.json"),
        Actions(
            mailbox,
            settings.output_dir,
            save_drafts=args.save_drafts,
            apply_labels=args.apply_labels,
            sender=build_sender(settings, args.source),
            send_categories=settings.send_categories,
            max_sends=settings.max_sends_per_run,
        ),
        own_address=own_address(settings, args.source),
    )
    only = set(args.only or [])
    start = time.perf_counter()
    report = agent.run(limit=settings.max_emails if only else args.limit, only=only)
    run_duration.record(time.perf_counter() - start)
    event(
        "run.finished",
        f"Run finished: {len(report.results)} emails, {agent.llm_calls} LLM calls, "
        f"{agent.tokens['input']}+{agent.tokens['output']} tokens, ~${agent.cost_usd:.4f}",
        **{
            "run.emails_processed": len(report.results),
            "run.llm_calls": agent.llm_calls,
            "run.input_tokens": agent.tokens["input"],
            "run.output_tokens": agent.tokens["output"],
            "run.cost_usd": round(agent.cost_usd, 6),
            "run.stopped_reason": agent.stopped_reason or "",
        },
    )
    print_report(report, getattr(mailbox, "expected", {}))
    if agent.not_found:
        print(f"Not found among fetched emails: {', '.join(agent.not_found)}")
    if agent.stopped_reason:
        print(f"Run stopped early: {agent.stopped_reason}\nRemaining emails will be processed on the next run.")
    return report


def wait_for_mail(watcher: ImapWatcher, interval: int) -> None:
    log.info("Waiting up to %s seconds for new mail", interval)
    with tracer.start_as_current_span("mailbox.wait") as span:
        arrived = watcher.wait(interval)
        span.set_attribute("mail.new", arrived)
    mail_wakeups.add(1, {"reason": "new_mail" if arrived else "timeout"})
    if arrived:
        event("mail.new", "New mail")
    else:
        event("mail.timeout", f"No new mail in {interval} seconds; checking anyway")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Classify emails and draft replies, with guardrails.")
    parser.add_argument(
        "--source", choices=["mock", "imap"], help="default: imap if IMAP_USERNAME is set, otherwise mock"
    )
    parser.add_argument(
        "--profile", help="provider settings from config/PROFILE.env: gmail, outlook, yahoo (default: MAIL_PROFILE)"
    )
    parser.add_argument("--limit", type=int, default=10)
    parser.add_argument(
        "--save-drafts",
        action=argparse.BooleanOptionalAction,
        default=True,
        help="save replies that are not sent as drafts (default: on)",
    )
    parser.add_argument(
        "--apply-labels",
        action=argparse.BooleanOptionalAction,
        help="add Triage/* labels, IMAP keywords outside Gmail (default: APPLY_LABELS, on)",
    )
    parser.add_argument("--watch", action="store_true", help="keep running and handle new mail as it arrives")
    parser.add_argument(
        "--interval", type=int, default=300, help="max seconds between checks (IMAP wakes early on new mail)"
    )
    parser.add_argument("--fresh", action="store_true", help="forget processed emails and handle all again")
    parser.add_argument("--only", nargs="+", metavar="ID", help="reprocess just these email ids, even if done")
    args = parser.parse_args(argv)

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    settings = load_settings(args.profile)
    args.source = args.source or ("imap" if settings.imap_username else "mock")
    if args.apply_labels is None:
        args.apply_labels = settings.apply_labels
    providers = setup_telemetry(settings.otel_traces_exporter)
    model = settings.gemini_model if settings.llm_provider == "gemini" else settings.hf_model
    prime_metrics(settings.llm_provider, model)
    for provider in providers or []:
        provider.force_flush()  # export the zero samples now, before the first email can count
    watcher = build_watcher(settings) if args.watch and args.source == "imap" else None
    try:
        while True:
            try:
                run_once(settings, args)
                if not args.watch:
                    return 0
                settings = load_settings(args.profile)  # picks up rotated secrets, e.g. a refreshed OAuth token
                if watcher:
                    watcher.account = build_account(settings)
                    wait_for_mail(watcher, args.interval)
                else:
                    log.info("Sleeping %s seconds", args.interval)
                    time.sleep(args.interval)
            except imaplib.IMAP4.error as exc:  # bad credentials etc. - stop instead of retrying forever
                raise SystemExit(f"IMAP login or command failed: {exc}") from None
    except KeyboardInterrupt:
        return 0
    finally:
        for provider in providers or []:
            provider.shutdown()  # flush spans, metrics and logs


if __name__ == "__main__":
    sys.exit(main())
