"""Behavioural anomaly model over execution traces. Advisory only.

Scores how unusual each agent's recent behaviour is compared with what the
model learned from recorded traffic, and explains which features drove the
score. It never allows or denies anything: enforcement stays deterministic
and outside any model (CLAUDE.md), and no enforcement module may import
this package (tests/test_ml.py checks this).
"""
