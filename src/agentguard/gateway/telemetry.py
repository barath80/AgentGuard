"""OpenTelemetry tracing for the gateway.

Without OTEL_EXPORTER_OTLP_ENDPOINT set, the API's default no-op tracer is
used and spans cost almost nothing. With it set (e.g. to the Jaeger
service in docker-compose's `observability` profile), spans are exported
over OTLP/HTTP.

Span attributes carry identifiers, decisions and outcomes only — never
tool arguments or results, which may contain customer data
(docs/THREAT_MODEL.md #9).
"""

import os

from opentelemetry import trace

tracer = trace.get_tracer("agentguard.gateway")


def configure_telemetry() -> bool:
    if not os.environ.get("OTEL_EXPORTER_OTLP_ENDPOINT"):
        return False

    from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter
    from opentelemetry.sdk.resources import Resource
    from opentelemetry.sdk.trace import TracerProvider
    from opentelemetry.sdk.trace.export import BatchSpanProcessor

    provider = TracerProvider(resource=Resource.create({"service.name": "agentguard-gateway"}))
    provider.add_span_processor(BatchSpanProcessor(OTLPSpanExporter()))
    trace.set_tracer_provider(provider)
    return True
