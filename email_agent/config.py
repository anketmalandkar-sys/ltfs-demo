"""All settings come from environment variables, a .env file, a provider profile (config/<name>.env)
or, for secrets, files in a secrets directory (Docker secrets). Secrets are SecretStr so they never
show up in logs, reprs or traces."""

from pathlib import Path
from typing import Literal

from pydantic import Field, SecretStr, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

from .models import Category


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    # LLM
    llm_provider: Literal["gemini", "huggingface", "openai"] = "gemini"
    gemini_api_key: SecretStr | None = None
    gemini_model: str = "gemini-2.5-flash"
    hf_token: SecretStr | None = None
    hf_model: str = "Qwen/Qwen2.5-72B-Instruct"
    openai_api_key: SecretStr | None = None
    openai_model: str = "gpt-4.1-mini"
    llm_requests_per_minute: int = Field(0, ge=0)  # 0 = no client-side limit; free Gemini tier needs ~5
    llm_timeout_seconds: float = Field(90, gt=0)  # per HTTP attempt; slower answers are retried
    # Cost estimate in USD per million tokens (your model's price list; 0 = not tracked, e.g. free tier)
    llm_input_price_per_mtok: float = Field(0, ge=0)
    llm_output_price_per_mtok: float = Field(0, ge=0)

    # Mailbox: any IMAP server over TLS (port 993). Provider defaults live in config/<profile>.env
    imap_host: str = ""
    imap_port: int = Field(993, ge=1, le=65535)
    imap_username: str = ""
    imap_auth: Literal["password", "xoauth2"] = "password"
    imap_password: SecretStr | None = None  # password or app password
    imap_oauth_token: SecretStr | None = None  # OAuth2 access token, for imap_auth=xoauth2
    imap_inbox: str = "INBOX"
    imap_drafts_folder: str = ""  # empty = find the folder flagged \Drafts
    mail_address: str = ""  # our own address (draft From, self-mail check); empty = imap_username
    mail_days: int = Field(7, ge=1, le=90)
    apply_labels: bool = True  # add Triage/* labels; the outlook profile turns this off (not supported there)

    # Sending replies: on unless AUTO_SEND=false. Only these categories (comma-separated), only replies
    # without any guardrail flag, at most MAX_SENDS_PER_RUN per run. Everything else stays a draft.
    auto_send: bool = True
    auto_send_categories: str = "customer_complaint,kyc_document"
    max_sends_per_run: int = Field(5, ge=0)
    smtp_host: str = ""  # login is the same as IMAP (username + password or OAuth token)
    smtp_port: int = Field(465, ge=1, le=65535)  # 465 = TLS, other ports use STARTTLS

    # Guardrail limits
    max_emails: int = Field(50, ge=1, le=500)
    max_steps: int = Field(6, ge=2, le=20)
    max_llm_calls_per_run: int = Field(200, ge=1)
    max_attempts: int = Field(3, ge=1)
    max_body_chars: int = Field(8000, ge=500)
    min_confidence: float = Field(0.6, ge=0, le=1)

    # Reply placeholders (filled by code, never by the LLM)
    ticket_prefix: str = "LTFS"
    sla_text: str = "within 2 business days"
    signature: str = "Regards,\nCustomer Care Team"

    # Files
    output_dir: Path = Path("output")
    mock_inbox: Path = Path("data/mock_inbox.json")

    # Tracing: none | console | otlp (OTLP endpoint comes from OTEL_EXPORTER_OTLP_ENDPOINT)
    otel_traces_exporter: Literal["none", "console", "otlp"] = "none"  # otlp sends traces, metrics and logs
    otel_capture_content: bool = False  # also record raw LLM prompts and replies (customer data!)

    @field_validator("auto_send_categories")
    @classmethod
    def _known_send_categories(cls, value: str) -> str:
        for name in filter(None, (part.strip() for part in value.split(","))):
            if name not in {c.value for c in Category} or name == Category.PHISHING_SUSPICIOUS.value:
                raise ValueError(f"AUTO_SEND_CATEGORIES: '{name}' is not a category that can be sent")
        return value

    @property
    def llm_model(self) -> str:
        """The model of the chosen LLM_PROVIDER."""
        return {"gemini": self.gemini_model, "huggingface": self.hf_model, "openai": self.openai_model}[
            self.llm_provider
        ]

    @property
    def send_categories(self) -> frozenset[Category]:
        return frozenset(Category(part.strip()) for part in self.auto_send_categories.split(",") if part.strip())
