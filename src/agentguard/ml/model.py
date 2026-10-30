"""Train, persist and score the behavioural anomaly model.

    python -m agentguard.ml.model train                 # from DATABASE_URL, read-only
    python -m agentguard.ml.model train --min-windows 30 --out models/anomaly.joblib
    python -m agentguard.ml.model score                 # latest risk per agent

Two detectors, and a window's risk is the more severe of the two:

- an Isolation Forest, which learns what normal per-agent 15-minute windows
  look like and catches unusual *combinations* of features; its score is
  calibrated as a percentile of the training windows;
- a robust per-feature deviation check (distance from the training median in
  units of the IQR), which catches values the training data never showed.
  A forest can't split on a feature that never varied in training — and in
  healthy traffic, tenant mismatches or schema rejections are always zero,
  so exactly the never-seen-before behaviour would otherwise be invisible.

Each score comes with the features that deviate most, so an operator can see *why*.

Limits, stated plainly: it learns "normal" from whatever traffic it was
trained on, so if that traffic already contained abuse, the abuse looks
normal; it needs enough windows to mean anything (training refuses below
--min-windows); and it is advisory — the gateway never consults it.
"""

import argparse
import hashlib
import sys
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import joblib
import numpy as np
from sklearn.ensemble import IsolationForest
from sqlalchemy.orm import Session

from agentguard.config import settings
from agentguard.ml.features import FEATURES, LOG_SCALED, WINDOW, Window, build_windows

MODEL_VERSION = 2
# Smallest deviation unit per feature, so a feature that never varied in training
# still has a meaningful scale: about one event (log1p(1) ≈ 0.69), or 10 points of a rate.
SCALE_FLOOR = np.array([0.5 if f in LOG_SCALED else 0.1 if f.endswith("_rate") else 1.0 for f in FEATURES])
ELEVATED_DEVIATION = 3.0
HIGH_DEVIATION = 6.0


class ModelUnavailable(Exception):
    pass


@dataclass
class TrainedModel:
    forest: IsolationForest
    median: np.ndarray
    scale: np.ndarray
    train_scores: np.ndarray  # sorted anomaly scores of the training windows, for percentile calibration
    trained_at: str
    windows: int
    agents: int

    def anomaly_scores(self, x: np.ndarray) -> np.ndarray:
        return -self.forest.score_samples(x)  # higher = more anomalous

    def percentile(self, score: float) -> float:
        return float(np.searchsorted(self.train_scores, score, side="right") / len(self.train_scores))

    def deviations(self, vector: list[float]) -> np.ndarray:
        return (np.asarray(vector) - self.median) / self.scale

    def explain(self, vector: list[float], top: int = 3) -> list[dict[str, Any]]:
        deviation = self.deviations(vector)
        order = np.argsort(-np.abs(deviation))[:top]
        return [{"feature": FEATURES[i], "deviation": round(float(deviation[i]), 2)} for i in order if abs(deviation[i]) >= 1]


def train(windows: list[Window], *, min_windows: int = 50, seed: int = 0) -> TrainedModel:
    if len(windows) < min_windows:
        raise ModelUnavailable(
            f"only {len(windows)} behaviour windows recorded; need at least {min_windows} to learn what normal looks like"
        )
    x = np.array([w.vector() for w in windows], dtype=float)
    median = np.median(x, axis=0)
    # Robust scale (IQR), floored so features that never varied still have a sensible unit.
    q75, q25 = np.percentile(x, [75, 25], axis=0)
    scale = np.maximum(q75 - q25, SCALE_FLOOR)
    forest = IsolationForest(n_estimators=300, contamination="auto", random_state=seed).fit(x)
    model = TrainedModel(
        forest=forest, median=median, scale=scale, train_scores=np.array([]),
        trained_at=datetime.now(UTC).isoformat(), windows=len(windows), agents=len({w.agent_id for w in windows}),
    )
    model.train_scores = np.sort(model.anomaly_scores(x))
    return model


def save(model: TrainedModel, path: Path) -> str:
    path.parent.mkdir(parents=True, exist_ok=True)
    joblib.dump({"version": MODEL_VERSION, "features": FEATURES, "model": model}, path)
    digest = hashlib.sha256(path.read_bytes()).hexdigest()
    path.with_suffix(path.suffix + ".sha256").write_text(digest)
    return digest


def load(path: Path) -> TrainedModel:
    """Load a model artifact. joblib files are pickles, so the artifact location must be
    writable only by whoever trains the model; the checksum catches corruption and
    mismatched files, not an attacker who can write both."""
    checksum = path.with_suffix(path.suffix + ".sha256")
    if not path.exists() or not checksum.exists():
        raise ModelUnavailable(f"no trained model at {path}")
    if hashlib.sha256(path.read_bytes()).hexdigest() != checksum.read_text().strip():
        raise ModelUnavailable("model artifact does not match its checksum")
    bundle = joblib.load(path)
    if bundle.get("version") != MODEL_VERSION or bundle.get("features") != FEATURES:
        raise ModelUnavailable("model was trained with a different feature set; retrain it")
    return bundle["model"]


LEVELS = ("normal", "elevated", "high")


def risk_level(percentile: float, max_deviation: float = 0.0) -> str:
    by_forest = 2 if percentile >= 0.99 else 1 if percentile >= 0.95 else 0
    by_deviation = 2 if max_deviation >= HIGH_DEVIATION else 1 if max_deviation >= ELEVATED_DEVIATION else 0
    return LEVELS[max(by_forest, by_deviation)]


def score_windows(model: TrainedModel, windows: list[Window]) -> list[dict[str, Any]]:
    if not windows:
        return []
    scores = model.anomaly_scores(np.array([w.vector() for w in windows], dtype=float))
    out = []
    for w, score in zip(windows, scores, strict=True):
        pct = model.percentile(float(score))
        # Only deviations upward count: doing *less* of something (fewer calls) isn't a risk signal.
        max_dev = float(np.max(model.deviations(w.vector()), initial=0.0))
        out.append({
            "agent_id": w.agent_id,
            "window_start": w.start.isoformat(),
            "score": round(float(score), 4),
            "percentile": round(pct, 4),
            "max_deviation": round(max_dev, 2),
            "risk": risk_level(pct, max_dev),
            "calls": int(w.values["calls"]),
            "drivers": model.explain(w.vector()),
            "execution_ids": w.execution_ids,
        })
    return out


def agent_risk(session: Session, model: TrainedModel, *, hours: int = 24) -> list[dict[str, Any]]:
    """Per agent: its riskiest window in the last `hours`."""
    since = datetime.now(UTC) - timedelta(hours=hours)
    worst: dict[str, dict[str, Any]] = {}

    def rank(row: dict[str, Any]) -> tuple[int, float]:
        return LEVELS.index(row["risk"]), row["score"]

    for row in score_windows(model, build_windows(session, since=since)):
        if row["agent_id"] not in worst or rank(row) > rank(worst[row["agent_id"]]):
            worst[row["agent_id"]] = row
    return sorted(worst.values(), key=rank, reverse=True)


def main(argv: list[str] | None = None) -> int:
    from agentguard.db import close_session, read_only_session

    parser = argparse.ArgumentParser(description="Behavioural anomaly model (advisory)")
    parser.add_argument("--database-url", default=settings.database_url)
    parser.add_argument("--model-path", type=Path, default=Path(settings.anomaly_model_path))
    sub = parser.add_subparsers(dest="command", required=True)
    tr = sub.add_parser("train", help="train on all recorded traces")
    tr.add_argument("--min-windows", type=int, default=50)
    sub.add_parser("score", help="show the riskiest recent window per agent")
    args = parser.parse_args(argv)

    session = read_only_session(args.database_url)
    try:
        if args.command == "train":
            windows = build_windows(session)
            try:
                model = train(windows, min_windows=args.min_windows)
            except ModelUnavailable as exc:
                print(f"not trained: {exc}", file=sys.stderr)
                return 1
            digest = save(model, args.model_path)
            print(f"trained on {model.windows} windows from {model.agents} agents ({WINDOW} each)")
            print(f"saved {args.model_path} (sha256 {digest[:12]}…)")
            top = sorted(score_windows(model, windows), key=lambda r: -r["score"])[:5]
            print("most unusual windows in the training data:")
            for row in top:
                drivers = ", ".join(f"{d['feature']} {d['deviation']:+}" for d in row["drivers"]) or "—"
                print(f"  {row['window_start']}  agent {row['agent_id'][:8]}  p{row['percentile']*100:.0f}  {drivers}")
            return 0
        try:
            model = load(args.model_path)
        except ModelUnavailable as exc:
            print(exc, file=sys.stderr)
            return 1
        for row in agent_risk(session, model):
            print(f"{row['agent_id'][:8]}  {row['risk']:<9} p{row['percentile']*100:.0f}  {row['drivers']}")
        return 0
    finally:
        close_session(session)


if __name__ == "__main__":
    sys.exit(main())
