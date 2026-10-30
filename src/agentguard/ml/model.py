"""Train, persist and score the behavioural anomaly model.

    python -m agentguard.ml.model train                 # from DATABASE_URL, read-only
    python -m agentguard.ml.model train --detector autoencoder --window-minutes 1
    python -m agentguard.ml.model score                 # latest risk per agent

The detector is chosen and evaluated in notebooks/anomaly_model.ipynb, which
compares the candidates in agentguard.ml.detectors on labelled traffic and
saves the winner here. Two signals, and a window's risk is the more severe:

- the learned detector (an Isolation Forest by default), which catches
  unusual *combinations* of features; its score is calibrated as a
  percentile of the training windows, and its elevated/high thresholds are
  stored with the model;
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
from sqlalchemy.orm import Session

from agentguard.config import settings
from agentguard.ml.detectors import DETECTORS, Detector
from agentguard.ml.features import FEATURES, LOG_SCALED, WINDOW, Window, build_windows

MODEL_VERSION = 4
# Smallest deviation unit per feature, so a feature that never varied in training
# still has a meaningful scale: about one event (log1p(1) ≈ 0.69), or 10 points of a rate.
SCALE_FLOOR = np.array([0.5 if f in LOG_SCALED else 0.1 if f.endswith("_rate") else 1.0 for f in FEATURES])
ELEVATED_DEVIATION = 3.0
HIGH_DEVIATION = 6.0


class ModelUnavailable(Exception):
    pass


@dataclass
class TrainedModel:
    detector: Detector
    median: np.ndarray
    scale: np.ndarray
    train_scores: np.ndarray  # sorted anomaly scores of the training windows, for percentile calibration
    trained_at: str
    windows: int
    agents: int
    window_seconds: float = WINDOW.total_seconds()
    # Detector scores at which a window becomes "elevated" / "high". By default the 95th and
    # 99th percentiles of the training windows; the notebook tunes them on held-out data.
    thresholds: tuple[float, float] = (float("inf"), float("inf"))
    metrics: dict[str, Any] | None = None  # held-out evaluation, when trained from the notebook
    # Whether the robust per-feature deviation check also raises risk. It catches never-seen
    # behaviour but can add false alarms; the notebook measures that and decides.
    use_deviation: bool = True

    @property
    def detector_name(self) -> str:
        return self.detector.name

    @property
    def window(self) -> timedelta:
        return timedelta(seconds=self.window_seconds)

    def anomaly_scores(self, x: np.ndarray) -> np.ndarray:
        return self.detector.score(x)  # higher = more anomalous

    def percentile(self, score: float) -> float:
        return float(np.searchsorted(self.train_scores, score, side="right") / len(self.train_scores))

    def deviations(self, vector: list[float]) -> np.ndarray:
        return (np.asarray(vector) - self.median) / self.scale

    def explain(self, vector: list[float], top: int = 3) -> list[dict[str, Any]]:
        deviation = self.deviations(vector)
        order = np.argsort(-np.abs(deviation))[:top]
        return [{"feature": FEATURES[i], "deviation": round(float(deviation[i]), 2)} for i in order if abs(deviation[i]) >= 1]


def train(
    windows: list[Window], *, min_windows: int = 50, seed: int = 0, detector: str | Detector = "isolation_forest",
    labels: list[int] | None = None, window: timedelta = WINDOW,
    thresholds: tuple[float, float] | None = None, metrics: dict[str, Any] | None = None,
) -> TrainedModel:
    """Fit a detector on behaviour windows. Supervised detectors need `labels`
    (1 = the window contains attack calls); unsupervised ones ignore them."""
    if len(windows) < min_windows:
        raise ModelUnavailable(
            f"only {len(windows)} behaviour windows recorded; need at least {min_windows} to learn what normal looks like"
        )
    x = np.array([w.vector() for w in windows], dtype=float)
    median = np.median(x, axis=0)
    # Robust scale (IQR), floored so features that never varied still have a sensible unit.
    q75, q25 = np.percentile(x, [75, 25], axis=0)
    scale = np.maximum(q75 - q25, SCALE_FLOOR)
    if isinstance(detector, str):
        if detector not in DETECTORS:
            raise ValueError(f"unknown detector {detector!r}; choose from {sorted(DETECTORS)}")
        detector = DETECTORS[detector](seed=seed)
    if detector.supervised:
        if labels is None or len(set(labels)) < 2:
            raise ModelUnavailable(f"{detector.name} is supervised: it needs labelled normal and attack windows")
        detector.fit(x, np.asarray(labels))
    else:
        detector.fit(x)
    model = TrainedModel(
        detector=detector, median=median, scale=scale, train_scores=np.array([]),
        trained_at=datetime.now(UTC).isoformat(), windows=len(windows), agents=len({w.agent_id for w in windows}),
        window_seconds=window.total_seconds(), metrics=metrics,
    )
    model.train_scores = np.sort(model.anomaly_scores(x))
    model.thresholds = thresholds or (
        float(np.quantile(model.train_scores, 0.95)), float(np.quantile(model.train_scores, 0.99)),
    )
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


def risk_level(score: float, thresholds: tuple[float, float], max_deviation: float = 0.0) -> str:
    elevated, high = thresholds
    by_detector = 2 if score >= high else 1 if score >= elevated else 0
    by_deviation = 2 if max_deviation >= HIGH_DEVIATION else 1 if max_deviation >= ELEVATED_DEVIATION else 0
    return LEVELS[max(by_detector, by_deviation)]


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
            "risk": risk_level(float(score), model.thresholds, max_dev if model.use_deviation else 0.0),
            "calls": int(w.values["calls"]),
            "drivers": model.explain(w.vector()),
            "execution_ids": w.execution_ids,
        })
    return out


def agent_risk(session: Session, model: TrainedModel, *, hours: int = 24) -> list[dict[str, Any]]:
    """Per agent: its riskiest window in the last `hours`, with how many of its windows were
    flagged, so one bad minute reads differently from an hour of them."""
    since = datetime.now(UTC) - timedelta(hours=hours)
    worst: dict[str, dict[str, Any]] = {}
    windows: dict[str, int] = {}
    flagged: dict[str, int] = {}

    def rank(row: dict[str, Any]) -> tuple[int, float]:
        return LEVELS.index(row["risk"]), row["score"]

    for row in score_windows(model, build_windows(session, since=since, window=model.window)):
        agent = row["agent_id"]
        windows[agent] = windows.get(agent, 0) + 1
        flagged[agent] = flagged.get(agent, 0) + (row["risk"] != "normal")
        if agent not in worst or rank(row) > rank(worst[agent]):
            worst[agent] = row
    rows = [{**row, "windows": windows[a], "flagged_windows": flagged[a]} for a, row in worst.items()]
    return sorted(rows, key=lambda r: (*rank(r), r["flagged_windows"]), reverse=True)


def main(argv: list[str] | None = None) -> int:
    from agentguard.db import close_session, read_only_session

    parser = argparse.ArgumentParser(description="Behavioural anomaly model (advisory)")
    parser.add_argument("--database-url", default=settings.database_url)
    parser.add_argument("--model-path", type=Path, default=Path(settings.anomaly_model_path))
    sub = parser.add_subparsers(dest="command", required=True)
    tr = sub.add_parser("train", help="train on recorded traces")
    tr.add_argument("--min-windows", type=int, default=50)
    tr.add_argument("--detector", default="isolation_forest",
                    help="an unsupervised detector from agentguard.ml.detectors (supervised ones train in the notebook)")
    tr.add_argument("--window-minutes", type=float, default=WINDOW.total_seconds() / 60)
    tr.add_argument("--since", type=datetime.fromisoformat, default=None,
                    help="only learn from traces after this time (ISO 8601, UTC): start of a period you trust")
    sub.add_parser("score", help="show the riskiest recent window per agent")
    args = parser.parse_args(argv)

    session = read_only_session(args.database_url)
    try:
        if args.command == "train":
            since = args.since.replace(tzinfo=args.since.tzinfo or UTC) if args.since else None
            window = timedelta(minutes=args.window_minutes)
            windows = build_windows(session, since=since, window=window)
            try:
                model = train(windows, min_windows=args.min_windows, detector=args.detector, window=window)
            except ModelUnavailable as exc:
                print(f"not trained: {exc}", file=sys.stderr)
                return 1
            digest = save(model, args.model_path)
            print(f"trained {model.detector_name} on {model.windows} windows from {model.agents} agents ({window} each)")
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
