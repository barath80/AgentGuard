"""Scoped credentials for the broker's executors.

Each value comes from AGENTGUARD_CRED_<SCOPE> (e.g. AGENTGUARD_CRED_BILLING_REFUND),
injected from a secrets manager in a real deployment and only into the broker's
own process. The synthetic `dev-only-<scope>` default exists for local runs and
tests; in production a missing credential is an error, never the public dev value. Nothing outside `agentguard.broker` may import this module — the agent,
SDK, replay and evaluation code never hold a credential that can act on a
target system (checked by tests/test_broker.py).
"""

import os

from agentguard.config import settings


class MissingCredential(RuntimeError):
    pass


def credential_for_scope(scope: str) -> str:
    env = "AGENTGUARD_CRED_" + scope.replace(":", "_").upper()
    value = os.environ.get(env)
    if value:
        return value
    if settings.environment == "production":
        raise MissingCredential(f"{env} is not set")
    return f"dev-only-{scope}"

