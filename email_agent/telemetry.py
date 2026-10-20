"""OpenTelemetry: traces, metrics and logs, exported over OTLP (to the Collector, which forwards them to
Tempo, Prometheus and Loki). Metadata is always recorded. Message content (LLM prompts, the email the
model reads, its replies) only when OTEL_CAPTURE_CONTENT=true. Credentials never: they are not part of
any message, and the Gemini key travels in a header."""

import json
import logging

from opentelemetry import _logs, metrics, trace
from opentelemetry.sdk._logs import LoggerProvider, LoggingHandler
from opentelemetry.sdk._logs.export import BatchLogRecordProcessor
from opentelemetry.sdk.metrics import MeterProvider
from opentelemetry.sdk.metrics.export import PeriodicExportingMetricReader
from opentelemetry.sdk.resources import Resource
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import BatchSpanProcessor, ConsoleSpanExporter, SimpleSpanProcessor

CONTENT_LIMIT = 32_000  # characters per captured prompt or completion
log = logging.getLogger("email_agent")
tracer = trace.get_tracer("email_agent")
meter = metrics.get_meter("email_agent")

# Prometheus names: counters get _total, durations _seconds (e.g. llm_request_duration_seconds)
emails_processed = meter.create_counter("emails_processed", description="Emails handled, by status and category")
guardrail_flags = meter.create_counter("guardrail_flags", description="Guardrail flags raised")
llm_requests = meter.create_counter("llm_requests", description="LLM calls, by outcome (ok, error, fatal)")
llm_request_duration = meter.create_histogram("llm_request_duration", unit="s", description="LLM call latency")
llm_tokens = meter.create_counter("llm_tokens", description="LLM tokens, by direction (input, output)")
llm_retries = meter.create_counter("llm_retries", description="Retried LLM HTTP attempts, by reason")
llm_cost = meter.create_counter("llm_cost", unit="USD", description="Estimated LLM cost (LLM_*_PRICE_PER_MTOK)")
replies_sent = meter.create_counter("replies_sent", description="Replies sent automatically (AUTO_SEND)")
drafts_saved = meter.create_counter("drafts_saved", description="Reply drafts saved")
mail_wakeups = meter.create_counter("mail_wakeups", description="Watch-loop wake-ups: new_mail or timeout")
run_duration = meter.create_histogram("run_duration", unit="s", description="Duration of one agent run")


def prime_metrics(provider: str, model: str) -> None:
    """Start every known counter series at 0. Prometheus' increase() needs an earlier sample, so a
    series whose first sample is already 1 would count as 0. (Guardrail flags are an open set; the
    dashboard counts those from the logs.)"""
    from .models import Category, Status

    for status in Status:
        for category in [*(c.value for c in Category), "none"]:
            emails_processed.add(0, {"status": status.value, "category": category})
    llm = {"provider": provider, "model": model}
    for outcome in ("ok", "error", "fatal"):
        llm_requests.add(0, {**llm, "outcome": outcome})
    for direction in ("input", "output"):
        llm_tokens.add(0, {**llm, "direction": direction})
    llm_cost.add(0, llm)
    for category in Category:
        replies_sent.add(0, {"category": category.value})
    drafts_saved.add(0)
    for reason in ("new_mail", "timeout"):
        mail_wakeups.add(0, {"reason": reason})


def setup_telemetry(exporter: str, set_global: bool = True) -> list | None:
    """exporter: 'none', 'console' (traces to stdout) or 'otlp' (traces, metrics and logs to
    OTEL_EXPORTER_OTLP_ENDPOINT). Returns the providers, so main can flush them on exit."""
    if exporter == "none":
        return None
    if exporter not in ("console", "otlp"):
        raise ValueError(f"unknown telemetry exporter '{exporter}'")
    resource = Resource.create({"service.name": "email-agent"})
    tracer_provider = TracerProvider(resource=resource)
    providers = [tracer_provider]
    if exporter == "console":
        tracer_provider.add_span_processor(SimpleSpanProcessor(ConsoleSpanExporter()))
    else:
        from opentelemetry.exporter.otlp.proto.http._log_exporter import OTLPLogExporter
        from opentelemetry.exporter.otlp.proto.http.metric_exporter import OTLPMetricExporter
        from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter

        tracer_provider.add_span_processor(BatchSpanProcessor(OTLPSpanExporter()))
        reader = PeriodicExportingMetricReader(OTLPMetricExporter(), export_interval_millis=15_000)
        logger_provider = LoggerProvider(resource=resource)
        logger_provider.add_log_record_processor(BatchLogRecordProcessor(OTLPLogExporter()))
        providers += [MeterProvider(resource=resource, metric_readers=[reader]), logger_provider]
    if set_global:
        from opentelemetry.instrumentation.requests import RequestsInstrumentor

        trace.set_tracer_provider(tracer_provider)
        if exporter == "otlp":
            metrics.set_meter_provider(providers[1])
            _logs.set_logger_provider(providers[2])
            log.addHandler(otel_log_handler(providers[2]))
        RequestsInstrumentor().instrument()  # HTTP spans for Gemini / Hugging Face calls
    return providers


def otel_log_handler(provider: LoggerProvider) -> LoggingHandler:
    """Sends email_agent log records to OpenTelemetry, with the current trace id and the `extra`
    fields as attributes (so Loki can filter on them and link to the trace in Tempo)."""
    return LoggingHandler(level=logging.INFO, logger_provider=provider)


def event(name: str, message: str, **attributes) -> None:
    """One searchable step: a readable message plus `event=<name>` and attributes."""
    log.info(message, extra={"event": name, **attributes})


def content(value) -> str:
    """Captured message content as JSON, capped at CONTENT_LIMIT characters."""
    text = json.dumps(value, ensure_ascii=False, default=str)
    return text if len(text) <= CONTENT_LIMIT else text[:CONTENT_LIMIT] + "…truncated"
