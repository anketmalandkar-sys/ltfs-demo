import pytest
from opentelemetry import metrics, trace
from opentelemetry.sdk.metrics import Counter, Histogram, MeterProvider
from opentelemetry.sdk.metrics.export import AggregationTemporality, InMemoryMetricReader
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter

# OpenTelemetry allows one global provider per process, so tests share these in-memory ones.
SPANS = InMemorySpanExporter()
_provider = TracerProvider()
_provider.add_span_processor(SimpleSpanProcessor(SPANS))
trace.set_tracer_provider(_provider)

# Delta temporality: each collection only holds what was recorded since the previous one.
METRICS = InMemoryMetricReader(
    preferred_temporality={Counter: AggregationTemporality.DELTA, Histogram: AggregationTemporality.DELTA}
)
metrics.set_meter_provider(MeterProvider(metric_readers=[METRICS]))


@pytest.fixture
def spans():
    SPANS.clear()
    return SPANS


class MetricValues:
    """recorded("llm_tokens", direction="input") -> sum of counter values (or histogram counts)
    recorded since the fixture was created."""

    def __init__(self):
        METRICS.get_metrics_data()  # drop what earlier tests recorded
        self.points = None

    def __call__(self, name: str, **attributes) -> float:
        if self.points is None:
            data = METRICS.get_metrics_data()
            self.points = [
                (m.name, p)
                for rm in (data.resource_metrics if data else [])
                for sm in rm.scope_metrics
                for m in sm.metrics
                for p in m.data.data_points
            ]
        return sum(
            getattr(p, "value", None) if hasattr(p, "value") else p.count
            for metric_name, p in self.points
            if metric_name == name and attributes.items() <= dict(p.attributes).items()
        )


@pytest.fixture
def recorded():
    return MetricValues()
