"""Optional: runs a few mock emails through a real LLM. Run with: pytest -m live
Needs GEMINI_API_KEY (or LLM_PROVIDER=huggingface + HF_TOKEN) in the environment or .env (keystore references work)."""

import json

import pytest

from email_agent.actions import Actions
from email_agent.agent import Agent
from email_agent.mailbox import MockMailbox
from email_agent.main import build_llm, load_settings
from email_agent.state import State

from .test_agent import EXPECTED

pytestmark = pytest.mark.live
SAMPLES = ["m01", "m03", "m05", "m08", "m11", "m14"]


def test_real_llm_classifies_samples(tmp_path):
    settings = load_settings(None).model_copy(update={"output_dir": tmp_path})
    if not (settings.gemini_api_key or settings.hf_token):
        pytest.skip("no LLM credentials configured")
    inbox = tmp_path / "inbox.json"
    inbox.write_text(json.dumps([EXPECTED[i] for i in SAMPLES]), encoding="utf-8")
    mailbox = MockMailbox(inbox, tmp_path / "drafts")
    agent = Agent(
        settings,
        mailbox,
        build_llm(settings),
        State(tmp_path / "state.json"),
        Actions(mailbox, tmp_path, save_drafts=True, apply_labels=False),
        own_address="me@x.local",
    )
    results = {r.email_id: r.status.value for r in agent.run(limit=10).results}
    hits = sum(results[i] == EXPECTED[i]["expected_status"] for i in SAMPLES)
    print(results)
    assert hits >= len(SAMPLES) - 1  # allow one disagreement from a real model
