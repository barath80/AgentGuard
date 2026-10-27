import pytest
from opentelemetry import trace
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter
from sqlalchemy.orm import Session

from agentguard.gateway import service as gateway_service
from agentguard.identity.service import resolve_identity
from agentguard.policy.seed import MOCK_AGENT_WORKLOAD_REF
from tests.conftest import tool_call


@pytest.fixture(scope="module")
def exporter():
    exporter = InMemorySpanExporter()
    provider = TracerProvider()
    provider.add_span_processor(SimpleSpanProcessor(exporter))
    trace.set_tracer_provider(provider)
    return exporter


def test_tool_call_emits_a_span_trajectory_without_payloads(session: Session, exporter):
    exporter.clear()
    secret_note = "customer card ends 4242"
    gateway_service.submit_tool_call(
        session,
        tool_call("issue-refund", {"ticket_id": "T-1001", "amount": 25, "note": secret_note}),
        resolve_identity(session, MOCK_AGENT_WORKLOAD_REF),
    )

    spans = {s.name: s for s in exporter.get_finished_spans()}
    assert {"agentguard.tool_call", "agentguard.policy.evaluate", "agentguard.broker.execute"} <= spans.keys()

    root = spans["agentguard.tool_call"]
    assert root.attributes["agentguard.decision"] == "allow"
    assert root.attributes["agentguard.outcome"] == "success"
    assert spans["agentguard.broker.execute"].parent.span_id == root.context.span_id

    all_attribute_values = [str(v) for s in spans.values() for v in s.attributes.values()]
    assert not any(secret_note in v or "4242" in v for v in all_attribute_values)
