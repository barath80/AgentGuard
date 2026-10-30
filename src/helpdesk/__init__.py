"""Helpdesk: the ticketing and billing system AgentGuard's tools act on.

A separate service with its own database and its own credential checks.
It knows nothing about AgentGuard (it must never import `agentguard`;
tests/test_helpdesk.py checks this) — it behaves like any third-party
API an agent platform would be wired to.
"""
