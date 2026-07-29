from dotenv import load_dotenv
load_dotenv()

import os
print("OTLP endpoint:", os.environ.get("OTEL_EXPORTER_OTLP_ENDPOINT"))

from opentelemetry import trace
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import BatchSpanProcessor
from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter

provider = TracerProvider()
provider.add_span_processor(BatchSpanProcessor(OTLPSpanExporter()))
trace.set_tracer_provider(provider)

tracer = trace.get_tracer("bookkeeper.nl-entry")
