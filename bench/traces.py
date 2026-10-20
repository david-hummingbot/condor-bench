"""Optional OpenTelemetry traces, exported to a local Phoenix collector.

**Off by default.** Set ``BENCH_TRACES=phoenix`` (or point ``BENCH_OTLP_ENDPOINT``
at any OTLP/HTTP collector) to turn it on. Nothing here is needed for a normal
run, and nothing is sent anywhere unless it is on.

When on, pydantic-ai's own instrumentation (model requests, tool calls, token
usage) is exported, and every trial runs inside a ``bench.case`` span carrying
the case id, trial, model and lane. The trace id lands on the case row
(``trace_id``, ``trace_url``) so a surprising score opens straight onto the
exchange that produced it. ACP models (Claude Code and friends) are not
pydantic-ai, so their rows get the ``bench.case`` span and nothing beneath it.

Start a collector with ``docker run -p 6006:6006 arizephoenix/phoenix`` or
``pip install arize-phoenix && phoenix serve``.
"""

from __future__ import annotations

import contextlib
import os
from typing import Any, Iterator

DEFAULT_PHOENIX_URL = "http://localhost:6006"
PROJECT_NAME = "condor-bench"

_tracer: Any = None
_configured = False


def enabled() -> bool:
    mode = (os.environ.get("BENCH_TRACES") or "").strip().lower()
    return mode in ("1", "true", "phoenix", "otlp") or bool(os.environ.get("BENCH_OTLP_ENDPOINT"))


def phoenix_url() -> str:
    return (os.environ.get("BENCH_PHOENIX_URL") or DEFAULT_PHOENIX_URL).rstrip("/")


def otlp_endpoint() -> str:
    return (os.environ.get("BENCH_OTLP_ENDPOINT") or f"{phoenix_url()}/v1/traces").rstrip("/")


def configure(*, exporter: Any = None) -> bool:
    """Install the tracer provider once per process. Returns whether tracing is on.

    *exporter* overrides the OTLP exporter (tests pass an in-memory one).
    """
    global _tracer, _configured
    if _configured:
        return _tracer is not None
    _configured = True
    if exporter is None and not enabled():
        return False

    from opentelemetry import trace
    from opentelemetry.sdk.resources import Resource
    from opentelemetry.sdk.trace import TracerProvider
    from opentelemetry.sdk.trace.export import BatchSpanProcessor, SimpleSpanProcessor
    from pydantic_ai import Agent
    from pydantic_ai.models.instrumented import InstrumentationSettings

    provider = TracerProvider(
        resource=Resource.create(
            {"service.name": PROJECT_NAME, "openinference.project.name": PROJECT_NAME}
        )
    )
    if exporter is None:
        from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter

        provider.add_span_processor(BatchSpanProcessor(OTLPSpanExporter(endpoint=otlp_endpoint())))
    else:
        provider.add_span_processor(SimpleSpanProcessor(exporter))
    trace.set_tracer_provider(provider)
    Agent.instrument_all(InstrumentationSettings(tracer_provider=provider))
    _tracer = provider.get_tracer("condor-bench")
    return True


def reset_for_tests() -> None:
    global _tracer, _configured
    _tracer, _configured = None, False


@contextlib.contextmanager
def case_span(case_id: str, *, trial: int, model: str, lane: str) -> Iterator[dict[str, str | None]]:
    """Run a trial inside a ``bench.case`` span. Yields ``{trace_id, trace_url}``.

    Both are None when tracing is off, so callers can stamp them unconditionally.
    """
    link: dict[str, str | None] = {"trace_id": None, "trace_url": None}
    if not configure():
        yield link
        return
    attributes = {"bench.case_id": case_id, "bench.trial": trial, "bench.model": model, "bench.lane": lane}
    with _tracer.start_as_current_span("bench.case", attributes=attributes) as span:
        trace_id = format(span.get_span_context().trace_id, "032x")
        link["trace_id"] = trace_id
        # Phoenix resolves an OTel trace id to its project page through this route.
        link["trace_url"] = f"{phoenix_url()}/redirects/traces/{trace_id}"
        yield link


def flush() -> None:
    """Export buffered spans before the process exits."""
    if _tracer is None:
        return
    from opentelemetry import trace

    provider = trace.get_tracer_provider()
    if hasattr(provider, "force_flush"):
        provider.force_flush()
