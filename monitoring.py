"""Tracing (OpenTelemetry). Off unless configured: with no OTEL_EXPORTER_OTLP_ENDPOINT and
no OPIK_API_KEY, spans go nowhere — nothing about traders' activity leaves the server.

To send traces to Opik (Comet):  OPIK_API_KEY=... OPIK_WORKSPACE=...
To any other OTLP collector:      OTEL_EXPORTER_OTLP_ENDPOINT=https://collector.example.com
"""
from dotenv import load_dotenv
load_dotenv()

import os
from opentelemetry import trace
from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import BatchSpanProcessor

OPIK_ENDPOINT = "https://www.comet.com/opik/api/v1/private/otel"


def _configure() -> None:
    endpoint = os.getenv("OTEL_EXPORTER_OTLP_ENDPOINT")
    opik_key = os.getenv("OPIK_API_KEY")
    if not endpoint and not opik_key:
        return  # the default no-op tracer: spans are created and dropped

    # Parse headers from OTEL_EXPORTER_OTLP_HEADERS (e.g. "Authorization=xxx,Comet-Workspace=yyy")
    headers = {}
    for item in os.environ.get("OTEL_EXPORTER_OTLP_HEADERS", "").split(","):
        if "=" in item:
            k, v = item.split("=", 1)
            headers[k.strip()] = v.strip()

    # Build headers required by Opik
    if opik_key:
        headers["Comet-Api-Key"] = opik_key
        headers["Authorization"] = opik_key
        headers["authorization"] = opik_key
    if workspace := os.getenv("OPIK_WORKSPACE"):
        headers["Comet-Workspace"] = workspace
        headers["opik-workspace"] = workspace

    endpoint = endpoint or OPIK_ENDPOINT
    if not endpoint.endswith("/v1/traces"):
        endpoint = endpoint.rstrip("/") + "/v1/traces"

    provider = TracerProvider()
    provider.add_span_processor(BatchSpanProcessor(OTLPSpanExporter(endpoint=endpoint, headers=headers)))
    trace.set_tracer_provider(provider)


_configure()
tracer = trace.get_tracer("bookkeeper.nl-entry")
