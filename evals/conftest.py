"""Options and shared objects for the eval run. Run: docker compose run --rm evals [--judge] [--max-calls N]"""

import pytest

from email_agent.main import build_llm, load_settings

from .metrics import AgentJudgeModel
from .page import write_page
from .report import Report
from .runner import CachedLLM, judge_settings

REPORT = Report()


def pytest_addoption(parser):
    group = parser.getgroup("evals")
    group.addoption("--judge", action="store_true", help="also grade replies with an LLM judge (DeepEval GEval)")
    group.addoption(
        "--judge-provider",
        choices=["gemini", "huggingface", "openai"],
        help="judge LLM provider (default: the agent's)",
    )
    group.addoption("--judge-model", help="judge model (default: the agent's); another model avoids self-grading")
    group.addoption(
        "--max-calls", type=int, default=15, help="new LLM requests allowed this run (free Gemini tier: ~20 a day)"
    )
    group.addoption("--fresh", action="store_true", help="ignore cached LLM replies and ask again")


@pytest.fixture(scope="session")
def settings():
    return load_settings(None)


@pytest.fixture(scope="session")
def budget(request):
    REPORT.max_calls = request.config.getoption("--max-calls")
    REPORT.budget = {"left": REPORT.max_calls, "stopped": None}
    return REPORT.budget


@pytest.fixture(scope="session")
def agent_llm(request, settings, budget):
    llm = CachedLLM(build_llm(settings), cache_dir(settings), budget, fresh=request.config.getoption("--fresh"))
    REPORT.model, REPORT.output_dir = f"{llm.system}/{llm.model}", settings.output_dir / "evals"
    return llm


@pytest.fixture(scope="session")
def judge(request, settings, budget):
    """None unless --judge."""
    if not request.config.getoption("--judge"):
        return None
    options = request.config.getoption
    settings = judge_settings(settings, options("--judge-provider"), options("--judge-model"))
    llm = CachedLLM(build_llm(settings), cache_dir(settings), budget, fresh=request.config.getoption("--fresh"))
    return AgentJudgeModel(llm)


def cache_dir(settings):
    return settings.output_dir / "evals" / "cache"


def pytest_terminal_summary(terminalreporter, config):
    if not REPORT.records:
        return
    terminalreporter.write_line("\n" + REPORT.text())
    path = REPORT.save()
    terminalreporter.write_line(f"\nReport: {path}\nPage:   {write_page(REPORT.output_dir)}  (open it in a browser)")
