"""Scoped credentials for the broker's executors.

Stand-in for a secrets manager: each value comes from
AGENTGUARD_MOCK_CRED_<SCOPE> if set, else a synthetic dev value. Nothing
outside `agentguard.broker` may import this module — the agent, SDK,
replay and evaluation code never hold a credential that can act on a
target system (checked by tests/test_broker.py).
"""

import os


def credential_for_scope(scope: str) -> str:
    env = "AGENTGUARD_MOCK_CRED_" + scope.replace(":", "_").upper()
    return os.environ.get(env, f"dev-only-{scope}")
