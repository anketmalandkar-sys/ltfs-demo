"""The judge probes first (they say whether the judge can be trusted), then one test per eval case."""

import pytest

from email_agent.llm import FatalLLMError

from .conftest import REPORT
from .metrics import as_test_case, measure, metrics_for, probe_as_test_case, reply_judge
from .runner import Pending, load_cases, run_case

CASES = [case for case in load_cases() if not case.get("judge_probe")]
PROBES = [case for case in load_cases() if case.get("judge_probe")]


@pytest.mark.parametrize("probe", PROBES, ids=lambda probe: probe["id"])
def test_judge_probe(probe, judge):
    """Hand-written replies, good and bad: the judge must pass and fail them as labeled."""
    if judge is None:
        pytest.skip("needs --judge")
    metric = reply_judge(judge)
    try:
        metric.measure(probe_as_test_case(probe))
    except FatalLLMError as exc:
        pytest.skip(f"pending: {exc}")
    agreed = bool(metric.success) == probe["expect"]["judge_pass"]
    REPORT.add_probe(probe, metric, agreed)
    verdict = "passed" if metric.success else "failed"
    assert agreed, f"the judge {verdict} a reply labeled judge_pass={probe['expect']['judge_pass']}: {metric.reason}"


@pytest.mark.parametrize("case", CASES, ids=lambda case: case["id"])
def test_case(case, settings, agent_llm, judge):
    try:
        result, llm_calls = run_case(case, settings, agent_llm)
    except Pending as exc:
        REPORT.add_pending(case, str(exc))
        pytest.skip(f"pending: {exc}")
    metrics = metrics_for(case, result, judge)
    failures = measure(metrics, as_test_case(case, result, settings.signature))
    REPORT.add(case, result, metrics, llm_calls)
    assert not failures, "\n".join(failures)
