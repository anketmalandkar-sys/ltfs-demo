import logging

import pytest
from opentelemetry.sdk._logs import LoggerProvider
from opentelemetry.sdk._logs.export import InMemoryLogExporter, SimpleLogRecordProcessor
from opentelemetry.sdk.trace.export import ConsoleSpanExporter

from email_agent.telemetry import CONTENT_LIMIT, content, event, otel_log_handler, setup_telemetry, tracer


def test_none_exporter_sets_nothing_up():
    assert setup_telemetry("none", set_global=False) is None


def test_console_exporter_only_traces():
    (provider,) = setup_telemetry("console", set_global=False)
    processors = provider._active_span_processor._span_processors
    assert isinstance(processors[0].span_exporter, ConsoleSpanExporter)
    assert provider.resource.attributes["service.name"] == "email-agent"


def test_otlp_exports_traces_metrics_and_logs(monkeypatch):
    monkeypatch.setenv("OTEL_EXPORTER_OTLP_ENDPOINT", "http://otel-collector:4318")
    traces, metrics, logs = setup_telemetry("otlp", set_global=False)
    assert traces._active_span_processor._span_processors[0].span_exporter._endpoint == (
        "http://otel-collector:4318/v1/traces"
    )
    assert metrics._sdk_config.metric_readers[0]._exporter._endpoint == "http://otel-collector:4318/v1/metrics"
    log_processor = logs._multi_log_record_processor._log_record_processors[0]
    assert log_processor._exporter._endpoint == "http://otel-collector:4318/v1/logs"
    for provider in (traces, metrics, logs):
        provider.shutdown()


def test_unknown_exporter_rejected():
    with pytest.raises(ValueError):
        setup_telemetry("zipkin", set_global=False)


def test_module_tracer_records_to_active_provider(spans):
    with tracer.start_as_current_span("unit"):
        pass
    assert [s.name for s in spans.get_finished_spans()] == ["unit"]


def test_events_become_otel_logs_linked_to_the_trace(spans):
    exporter = InMemoryLogExporter()
    provider = LoggerProvider()
    provider.add_log_record_processor(SimpleLogRecordProcessor(exporter))
    handler = otel_log_handler(provider)
    logger = logging.getLogger("email_agent")
    logger.addHandler(handler)
    logger.setLevel(logging.INFO)  # main.py does this with logging.basicConfig
    try:
        with tracer.start_as_current_span("unit") as span:
            event("email.decided", "Email m08: drafted", **{"email.id": "m08", "guardrail.flags": ["a", "b"]})
    finally:
        logger.removeHandler(handler)
        logger.setLevel(logging.NOTSET)
    (record,) = (r.log_record for r in exporter.get_finished_logs())
    assert record.body == "Email m08: drafted"
    assert record.attributes["event"] == "email.decided" and record.attributes["email.id"] == "m08"
    assert list(record.attributes["guardrail.flags"]) == ["a", "b"]
    assert record.trace_id == span.get_span_context().trace_id


def test_content_is_json_and_capped():
    assert content({"a": "é"}) == '{"a": "é"}'
    long = content({"body": "x" * (CONTENT_LIMIT + 10)})
    assert len(long) <= CONTENT_LIMIT + 20 and long.endswith("…truncated")


def test_requests_instrumentation_can_be_imported():
    """Tracing loads it. Up to 0.48b0 it imported pkg_resources, which current setuptools (and the
    python:3.12-slim image) no longer ship - so this guards against a downgrade."""
    from opentelemetry.instrumentation.requests import RequestsInstrumentor  # noqa: F401


def test_prime_metrics_starts_known_series_at_zero(recorded):
    from email_agent.telemetry import prime_metrics

    prime_metrics(provider="gemini", model="m")
    assert recorded("emails_processed") == 0  # also collects recorded.points
    names = {name for name, _ in recorded.points}
    assert {"emails_processed", "llm_requests", "llm_tokens", "replies_sent", "drafts_saved", "mail_wakeups"} <= names
    combos = {tuple(sorted(p.attributes.items())) for name, p in recorded.points if name == "emails_processed"}
    assert (("category", "kyc_document"), ("status", "drafted")) in combos
