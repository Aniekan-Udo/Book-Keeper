from dotenv import load_dotenv
load_dotenv()

import os
from opentelemetry import trace
from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import BatchSpanProcessor


# Parse headers from OTEL_EXPORTER_OTLP_HEADERS (e.g. "Authorization=xxx,Comet-Workspace=yyy")
headers_str = os.environ.get("OTEL_EXPORTER_OTLP_HEADERS", "")
headers = {}
if headers_str:
    for item in headers_str.split(","):
        if "=" in item:
            k, v = item.split("=", 1)
            headers[k.strip()] = v.strip()

# Build headers required by Opik
if api_key := os.getenv("OPIK_API_KEY"):
    headers["Comet-Api-Key"] = api_key
    headers["Authorization"] = api_key
    headers["authorization"] = api_key
if workspace := os.getenv("OPIK_WORKSPACE"):
    headers["Comet-Workspace"] = workspace
    headers["opik-workspace"] = workspace

otlp_endpoint = os.getenv(
    "OTEL_EXPORTER_OTLP_ENDPOINT",
    "https://www.comet.com/opik/api/v1/private/otel"
)
if not otlp_endpoint.endswith("/v1/traces"):
    otlp_endpoint = otlp_endpoint.rstrip("/") + "/v1/traces"


provider = TracerProvider()
exporter = OTLPSpanExporter(
    endpoint=otlp_endpoint,
    headers=headers
)
provider.add_span_processor(BatchSpanProcessor(exporter))
trace.set_tracer_provider(provider)

tracer = trace.get_tracer("bookkeeper.nl-entry")
