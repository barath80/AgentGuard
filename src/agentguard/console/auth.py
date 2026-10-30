"""Console authentication: who is the human behind a console request.

Console callers are operators and approvers, not agents, so they are
authenticated separately from workload identity. A bearer token maps to
a configured principal; only SHA-256 hashes of tokens are configured
(`CONSOLE_PRINCIPALS`), so the server's environment never holds a usable
token. An approver's name, role and tenant come from that principal —
never from request headers or body — which is what keeps approval
authority from being self-asserted (docs/THREAT_MODEL.md #12).

Two ways in, same Principal out:

- OIDC (production): a JWT access token from the identity provider
  (Keycloak in this repo), verified against the issuer's published keys —
  signature, issuer, audience and expiry. Name, roles and tenants come from
  its claims; roles outside OIDC_ALLOWED_ROLES are ignored, and a token with
  no tenants claim can read nothing.
- Static tokens (`CONSOLE_PRINCIPALS`, `OPERATOR_TOKEN`): for local dev and
  break-glass access; disable with CONSOLE_STATIC_TOKENS=false.
"""

import hashlib
import hmac
import json
import logging
from dataclasses import dataclass
from functools import lru_cache

import jwt

from agentguard.config import settings

log = logging.getLogger(__name__)

ALL_TENANTS = "*"
OPERATOR_ROLE = "operator"  # may suspend/resume agents
VIEWER_ROLE = "viewer"  # read-only, never sees request payloads


class AuthError(Exception):
    pass


@dataclass(frozen=True)
class Principal:
    name: str
    roles: frozenset[str]
    # Tenants this principal may read. "*" grants read access everywhere but
    # never approval authority: approving needs the tenant named explicitly.
    tenants: frozenset[str]

    @property
    def reads_all_tenants(self) -> bool:
        return ALL_TENANTS in self.tenants

    def can_read_tenant(self, tenant_id: str) -> bool:
        return self.reads_all_tenants or tenant_id in self.tenants

    def can_approve_for(self, tenant_id: str, required_role: str | None) -> bool:
        if tenant_id not in self.tenants or self.roles <= {VIEWER_ROLE}:
            return False
        return required_role is None or required_role in self.roles

    @property
    def can_operate(self) -> bool:
        return OPERATOR_ROLE in self.roles

    @property
    def can_view_payloads(self) -> bool:
        return bool(self.roles - {VIEWER_ROLE})


def hash_token(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def _configured_principals() -> list[tuple[str, Principal]]:
    principals = []
    if settings.console_principals:
        try:
            entries = json.loads(settings.console_principals)
            for entry in entries:
                principals.append((
                    entry["token_sha256"].lower(),
                    Principal(entry["name"], frozenset(entry["roles"]), frozenset(entry["tenants"])),
                ))
        except (ValueError, KeyError, TypeError) as exc:
            # Fail closed: a malformed principal list signs nobody in.
            log.error("CONSOLE_PRINCIPALS is malformed: %s", exc)
            raise AuthError("console sign-in is misconfigured") from exc
    if settings.operator_token:
        principals.append((
            hash_token(settings.operator_token),
            Principal("operator", frozenset({OPERATOR_ROLE}), frozenset({ALL_TENANTS})),
        ))
    return principals


@lru_cache(maxsize=4)
def _jwks_client(url: str) -> jwt.PyJWKClient:
    # Caches the issuer's keys and refetches on an unknown key id (key rotation).
    return jwt.PyJWKClient(url, cache_keys=True, lifespan=3600)


def jwks_url() -> str:
    return settings.oidc_jwks_url or f"{settings.oidc_issuer.rstrip('/')}/protocol/openid-connect/certs"


def _from_oidc(token: str) -> Principal:
    try:
        key = _jwks_client(jwks_url()).get_signing_key_from_jwt(token).key
        claims = jwt.decode(
            token, key, algorithms=["RS256", "ES256"], issuer=settings.oidc_issuer,
            audience=settings.oidc_client_id, options={"require": ["exp", "iat", "iss", "sub"]},
        )
    except jwt.PyJWTError as exc:
        raise AuthError(f"invalid identity token: {exc}") from exc
    allowed = {r.strip() for r in settings.oidc_allowed_roles.split(",") if r.strip()}
    roles = set(claims.get("roles") or (claims.get("realm_access") or {}).get("roles") or []) & allowed
    tenants = claims.get("tenants") or []
    if isinstance(tenants, str):
        tenants = [tenants]
    name = claims.get("preferred_username") or claims.get("email") or claims["sub"]
    return Principal(name, frozenset(roles), frozenset(tenants))


def authenticate(authorization: str | None) -> Principal:
    scheme, _, token = (authorization or "").partition(" ")
    if scheme.lower() != "bearer" or not token:
        raise AuthError("missing bearer token")
    token = token.strip()
    if settings.oidc_issuer and token.count(".") == 2:
        return _from_oidc(token)
    if not settings.console_static_tokens:
        raise AuthError("invalid token")
    digest = hash_token(token)
    match = None
    for known, principal in _configured_principals():
        # Compare against every entry so timing doesn't reveal which one matched.
        if hmac.compare_digest(known, digest) and match is None:
            match = principal
    if match is None:
        raise AuthError("invalid token")
    return match
