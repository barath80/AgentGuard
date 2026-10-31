"""Policy shadow mode: a proposed policy evaluated next to the live one on real traffic.

Counterfactual simulation (agentguard.replay) answers "what would this policy have done to the
calls we already recorded?". Shadow mode answers the same question going forward, on live
traffic as it arrives, including calls no recorded history covers. Every call the live policy
evaluates also gets the shadow's decision recorded beside it (`executions.shadow_decision`).

The shadow never decides anything. `decide` is called after the live decision exists, its
result is only stored, and any error in it is swallowed: a broken proposal must not be able to
affect, slow down or fail a live call.

Which shadow is active is cached per process for `shadow_cache_seconds`, so starting or
stopping one takes up to that long to reach every replica. That's acceptable only because
the shadow is observational; the live policy is never cached this way.
"""

import logging
import threading
import time
from dataclasses import dataclass

from sqlalchemy.orm import Session

from agentguard.config import settings
from agentguard.models import ShadowPolicy
from agentguard.policy.engine import PolicyContext, RuleSpec, evaluate_rules

log = logging.getLogger(__name__)


@dataclass(frozen=True)
class ActiveShadow:
    id: str
    rules: list[RuleSpec]


_cache: tuple[float, ActiveShadow | None] | None = None
_lock = threading.Lock()


def rules_from_json(raw: list[dict]) -> list[RuleSpec]:
    return sorted(
        (RuleSpec(r["id"], r.get("match", {}), r["decision"], int(r["priority"]), r.get("approval_role"))
         for r in raw),
        key=lambda r: r.priority,
    )


def rules_to_json(rules: list[RuleSpec]) -> list[dict]:
    return [{"id": r.id, "match": r.match, "decision": r.decision, "priority": r.priority,
             "approval_role": r.approval_role} for r in rules]


def invalidate() -> None:
    global _cache
    with _lock:
        _cache = None


def active_shadow(session: Session) -> ActiveShadow | None:
    global _cache
    ttl = settings.shadow_cache_seconds
    with _lock:
        if ttl > 0 and _cache and time.monotonic() - _cache[0] < ttl:
            return _cache[1]
    row = session.query(ShadowPolicy).filter(ShadowPolicy.stopped_at.is_(None)).order_by(
        ShadowPolicy.started_at.desc()).first()
    shadow = ActiveShadow(row.id, rules_from_json(row.rules)) if row else None
    with _lock:
        _cache = (time.monotonic(), shadow)
    return shadow


def decide(session: Session, ctx: PolicyContext) -> tuple[str, str] | None:
    """(shadow policy id, what it would decide), or None without an active shadow.
    Never raises: shadow mode must not be able to affect a live call."""
    try:
        shadow = active_shadow(session)
        if shadow is None:
            return None
        return shadow.id, evaluate_rules(shadow.rules, -1, ctx).decision
    except Exception:  # observational path: the live decision is already made
        log.exception("shadow policy evaluation failed; live decision unaffected")
        return None
