"""Agent authentication with OAuth 2.0 client-credentials tokens (production mode).

Each agent is a confidential client at the identity provider (Keycloak here). It exchanges its
client secret for a short-lived access token and sends it as `Authorization: Bearer ...`. The
gateway verifies the token's signature against the issuer's published keys, its issuer,
audience and expiry, and that it was issued to a *service account*, not to a person signing in
through some client. The client id then names the registered agent.

Nothing the agent sends in the request body or other headers can change who it is: the
`x-workload-identity` header is ignored in this mode.
"""

from functools import lru_cache

import jwt

from agentguard.config import settings


class TokenError(Exception):
    pass


@lru_cache(maxsize=4)
def _jwks_client(url: str) -> jwt.PyJWKClient:
    # Caches the issuer's keys and refetches on an unknown key id (key rotation).
    return jwt.PyJWKClient(url, cache_keys=True, lifespan=3600)


def _jwks_url() -> str:
    return settings.oidc_jwks_url or f"{(settings.oidc_issuer or '').rstrip('/')}/protocol/openid-connect/certs"


def workload_identity_from_token(authorization: str | None) -> str:
    """The verified client id from a bearer token, or TokenError. Fails closed on any doubt."""
    if not settings.oidc_issuer:
        raise TokenError("agent token authentication is enabled but no OIDC issuer is configured")
    scheme, _, token = (authorization or "").partition(" ")
    if scheme.lower() != "bearer" or not token:
        raise TokenError("missing bearer token")
    try:
        key = _jwks_client(_jwks_url()).get_signing_key_from_jwt(token).key
        claims = jwt.decode(
            token, key, algorithms=["RS256", "ES256"], issuer=settings.oidc_issuer,
            audience=settings.agent_audience, options={"require": ["exp", "iat", "iss", "sub", "azp"]},
        )
    except jwt.PyJWTError as exc:
        raise TokenError(f"invalid agent token: {exc}") from exc
    client_id = claims["azp"]
    # Keycloak names a client's service-account user "service-account-<client id>". A person's
    # token issued to the same client has their own username and must not count as the agent.
    if claims.get("preferred_username") != f"service-account-{client_id}":
        raise TokenError("token was not issued to the agent's service account")
    return client_id
