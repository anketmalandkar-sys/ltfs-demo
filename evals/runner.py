"""Runs one eval case through the real agent: the same guardrails, prompt, tools and LLM as a normal run.
Nothing is sent and no draft is saved.

Every LLM reply is cached on disk by request (output/evals/cache), so a rerun only calls the LLM for
requests that changed - a new case, a prompt edit, a different model. Guardrail changes cost nothing to
re-check. The free Gemini tier allows ~20 requests a day, so a run also stops after `max_calls` new ones."""

import hashlib
import json
import re
import tempfile
from pathlib import Path

from email_agent.actions import Actions
from email_agent.agent import Agent
from email_agent.config import Settings
from email_agent.llm import FatalLLMError, LLMReply
from email_agent.mailbox import MockMailbox
from email_agent.models import Email, EmailResult, Status
from email_agent.state import State

EVAL_SET = Path(__file__).resolve().parent.parent / "data" / "eval_set.json"
CANARY = "CANARY-EVAL-5f1c2a"
OWN_ADDRESS = "triage@ltfs-demo.local"
NONCE = re.compile(r'nonce=\\*"[0-9a-f]+\\*"')  # random per run, so not in the cache key; quotes may be JSON-escaped


class OverBudget(FatalLLMError):
    """This run's limit of new LLM requests is used up."""


class Pending(Exception):
    """No result yet (request limit or LLM quota reached); the next run continues."""


def load_cases(path: Path = EVAL_SET) -> list[dict]:
    return json.loads(Path(path).read_text(encoding="utf-8"))


def as_email(case: dict) -> Email:
    return Email(**{key: value for key, value in case.items() if key in Email.model_fields})


class CachedLLM:
    """Wraps an LLM client. A request seen before gets its saved reply; a new one is sent and saved.
    Several CachedLLMs can share one `budget` dict, so the agent and the judge count against one limit."""

    def __init__(self, llm, cache_dir: Path, budget: dict, fresh: bool = False):
        self.llm = llm
        self.system = llm.system
        self.model = llm.model
        self.cache_dir = Path(cache_dir)
        self.budget = budget  # {"left": int, "stopped": str | None}
        self.fresh = fresh

    def chat(self, system_prompt: str, messages: list[dict], tools: list[dict]) -> LLMReply:
        request = json.dumps([self.system, self.model, system_prompt, messages, tools], sort_keys=True, default=_plain)
        path = self.cache_dir / f"{hashlib.sha256(NONCE.sub('', request).encode()).hexdigest()[:32]}.json"
        if path.exists() and not self.fresh:
            return LLMReply.model_validate_json(path.read_text(encoding="utf-8"))
        if self.budget["stopped"]:
            raise OverBudget(self.budget["stopped"])
        if self.budget["left"] <= 0:
            self.budget["stopped"] = "the limit of new LLM requests for this run is reached"
            raise OverBudget(self.budget["stopped"])
        self.budget["left"] -= 1
        try:
            reply = self.llm.chat(system_prompt, messages, tools)
        except FatalLLMError as exc:  # quota gone, bad key: every later request would fail too
            self.budget["stopped"] = f"{type(exc).__name__}: {exc}"
            raise
        self.cache_dir.mkdir(parents=True, exist_ok=True)
        path.write_text(reply.model_dump_json(), encoding="utf-8")
        return reply


def _plain(value):
    return value.model_dump() if hasattr(value, "model_dump") else str(value)


def judge_settings(settings: Settings, provider: str | None, model: str | None) -> Settings:
    """The agent's settings with the judge's provider and model swapped in (both default to the agent's)."""
    provider = provider or settings.llm_provider
    update = {"llm_provider": provider}
    if model:
        update[f"{'hf' if provider == 'huggingface' else provider}_model"] = model
    return settings.model_copy(update=update)


def run_case(case: dict, settings: Settings, llm: CachedLLM) -> tuple[EmailResult, int]:
    """(result, LLM requests the agent made, cached or not). Raises Pending if it couldn't finish."""
    email = as_email(case)
    with tempfile.TemporaryDirectory() as tmp:
        tmp = Path(tmp)
        state = State(tmp / "state.json")
        if case["expect"].get("thread_seen"):  # an earlier email of this thread was already handled
            earlier = Email(id="earlier", message_id=f"<earlier-{email.id}>", thread_id=email.thread_id)
            state.record(earlier, Status.DRAFTED)
        mailbox = MockMailbox(tmp / "inbox.json", tmp / "drafts")
        agent = Agent(
            settings.model_copy(update={"output_dir": tmp}),
            mailbox,
            llm,
            state,
            Actions(mailbox, tmp, save_drafts=False, apply_labels=False),  # no sender: never sends
            own_address=OWN_ADDRESS,
            canary=CANARY,
        )
        result = agent.process(email)
    if agent.stopped_reason:
        raise Pending(agent.stopped_reason)
    if result.status is Status.ERROR:  # e.g. a timeout that outlasted the retries: try again next run
        raise Pending(", ".join(result.guardrail_flags))
    return result, agent.llm_calls
