"""The only actions the LLM can take. Each tool = Pydantic args model + handler.

Least privilege: the LLM sees only the one email it was given (in the first message, wrapped by
render_email) and can only submit a triage and a reply. It cannot list, send, forward, fetch URLs
or touch any other email. Saving drafts and labels is done later by agent code, after the
guardrails approve."""

from typing import Literal

from pydantic import BaseModel, Field, ValidationError

from .models import Category, Email, ExtractedFields, Priority, ReplyDraft, Triage

TriageCategory = Literal[
    "incident",
    "bank_statement",
    "financial_disbursement",
    "payment_request",
    "customer_complaint",
    "kyc_document",
    "phishing_suspicious",
    "out_of_scope",
]
NO_REPLY = {Category.BANK_STATEMENT, Category.PHISHING_SUSPICIOUS}


class SubmitTriageArgs(BaseModel):
    email_id: str
    category: TriageCategory
    confidence: float = Field(ge=0, le=1, description="0..1, how sure you are")
    priority: Priority | None = Field(None, description="P1 (critical) .. P4 (low); incidents and complaints")
    summary: str = Field(max_length=300, description="One neutral sentence")
    fields: ExtractedFields = Field(ExtractedFields(), description="Only values literally present in the email")


class SubmitReplyArgs(BaseModel):
    email_id: str
    subject: str = Field(max_length=200)
    body: str = Field(max_length=1200, description="Plain text. Use {ticket_id}, {sla}, {signature} placeholders")


TOOLS = {
    "submit_triage": (
        SubmitTriageArgs,
        "Submit the classification. Call exactly once. Use out_of_scope for anything not in the category list.",
    ),
    "submit_reply": (
        SubmitReplyArgs,
        "Submit the reply draft. Call once, after submit_triage. "
        "Not allowed for bank_statement, phishing_suspicious or out_of_scope.",
    ),
}


def render_email(email: Email, nonce: str) -> str:
    """Wrap the email in a random-nonce tag; any look-alike tags inside are escaped."""

    def escape(text: str) -> str:
        return text.replace("<email_content", "&lt;email_content").replace("</email_content", "&lt;/email_content")

    attachments = ", ".join(email.attachment_names) or "none"
    return (
        f'<email_content nonce="{nonce}">\n'
        f"From: {escape(email.from_name)} <{escape(email.from_addr)}>\n"
        f"Subject: {escape(email.subject)}\n"
        f"Attachments: {escape(attachments)}\n"
        f"Body:\n{escape(email.body)}\n"
        f'</email_content nonce="{nonce}">'
    )


class ToolSession:
    """Tool state for one email. Every call returns a string that goes back to the LLM."""

    def __init__(self, email: Email, nonce: str):
        self.email = email
        self.nonce = nonce
        self.triage: Triage | None = None
        self.out_of_scope = False
        self.draft: ReplyDraft | None = None

    @property
    def done(self) -> bool:
        if self.out_of_scope or self.draft:
            return True
        return self.triage is not None and self.triage.category in NO_REPLY

    def call(self, name: str, args: dict) -> str:
        if name not in TOOLS:
            return f"error: unknown tool '{name}'"
        args_model, _ = TOOLS[name]
        try:
            parsed = args_model.model_validate(args)
        except ValidationError as exc:
            problems = "; ".join(f"{'.'.join(map(str, e['loc']))}: {e['msg']}" for e in exc.errors())
            return f"error: invalid arguments: {problems}"
        if parsed.email_id != self.email.id:
            return f"error: email_id must be '{self.email.id}'"
        return getattr(self, f"_{name}")(parsed)

    def prompt(self) -> str:
        """The first (and only) user message: the task plus the wrapped email."""
        return f"Triage the email with id '{self.email.id}':\n{render_email(self.email, self.nonce)}"

    def _submit_triage(self, args: SubmitTriageArgs) -> str:
        if self.triage or self.out_of_scope:
            return "error: triage already submitted"
        if args.category == "out_of_scope":
            self.out_of_scope = True
            return "ok: marked out of scope. Stop now."
        self.triage = Triage(
            category=args.category,
            confidence=args.confidence,
            priority=args.priority,
            summary=args.summary,
            fields=args.fields,
        )
        if self.done:
            return "ok: no reply is needed for this category. Stop now."
        return "ok: now call submit_reply."

    def _submit_reply(self, args: SubmitReplyArgs) -> str:
        if not self.triage:
            return "error: call submit_triage first"
        if self.triage.category in NO_REPLY:
            return f"error: no reply allowed for {self.triage.category.value}"
        if self.draft:
            return "error: reply already submitted"
        self.draft = ReplyDraft(subject=args.subject, body=args.body)
        return "ok: reply recorded. Stop now."


# --------------------------------------------------------------- schemas ----


def _flatten(node, defs: dict, gemini: bool):
    """Inline $refs and drop keys LLM APIs don't need (Gemini accepts only a subset of JSON Schema)."""
    if isinstance(node, list):
        return [_flatten(item, defs, gemini) for item in node]
    if not isinstance(node, dict):
        return node
    if "$ref" in node:
        return _flatten(defs[node["$ref"].rsplit("/", 1)[-1]], defs, gemini)
    drop = {"title", "default", "$defs"} | ({"additionalProperties"} if gemini else set())
    out = {key: _flatten(value, defs, gemini) for key, value in node.items() if key not in drop}
    if gemini and "anyOf" in out:  # Optional[X] -> X + nullable
        options = [option for option in out.pop("anyOf") if option.get("type") != "null"]
        out.update(options[0])
        out["nullable"] = True
    return out


def tool_schemas(dialect: str) -> list[dict]:
    """Function declarations for 'gemini' or 'openai' (Hugging Face router) style APIs."""
    schemas = []
    for name, (args_model, description) in TOOLS.items():
        schema = args_model.model_json_schema()
        parameters = _flatten(schema, schema.get("$defs", {}), gemini=dialect == "gemini")
        schemas.append({"name": name, "description": description, "parameters": parameters})
    return schemas
