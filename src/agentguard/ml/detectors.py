"""Candidate detectors for the behavioural anomaly model.

Every detector has the same interface — `fit(x, y=None)` and `score(x)`, where
a higher score means more anomalous — so the training notebook
(notebooks/anomaly_model.ipynb) can compare them on equal terms and the
winner can be saved and served by agentguard.ml.model unchanged. They live
in a module, not in the notebook, because a saved model can only be loaded
where its classes can be imported.

Unsupervised detectors learn "normal" from unlabelled history. Supervised
ones learn from labelled attacks: usually more accurate on attacks like the
ones they were trained on, and blind to kinds they never saw.
"""

from typing import ClassVar

import numpy as np
from sklearn.ensemble import HistGradientBoostingClassifier, IsolationForest, RandomForestClassifier
from sklearn.neighbors import LocalOutlierFactor
from sklearn.neural_network import MLPClassifier, MLPRegressor
from sklearn.pipeline import make_pipeline
from sklearn.preprocessing import StandardScaler
from sklearn.svm import OneClassSVM


class Detector:
    name: ClassVar[str]
    supervised: ClassVar[bool] = False

    def fit(self, x: np.ndarray, y: np.ndarray | None = None) -> "Detector":
        raise NotImplementedError

    def score(self, x: np.ndarray) -> np.ndarray:
        raise NotImplementedError


class IsolationForestDetector(Detector):
    name = "isolation_forest"

    def __init__(self, n_estimators: int = 300, seed: int = 0):
        self.model = IsolationForest(n_estimators=n_estimators, contamination="auto", random_state=seed)

    def fit(self, x, y=None):
        self.model.fit(x)
        return self

    def score(self, x):
        return -self.model.score_samples(x)


class LocalOutlierDetector(Detector):
    name = "local_outlier_factor"

    def __init__(self, n_neighbors: int = 20, seed: int = 0):  # deterministic; seed kept for a uniform signature
        self.model = make_pipeline(StandardScaler(), LocalOutlierFactor(n_neighbors=n_neighbors, novelty=True))

    def fit(self, x, y=None):
        self.model.fit(x)
        return self

    def score(self, x):
        return -self.model.score_samples(x)


class OneClassSVMDetector(Detector):
    name = "one_class_svm"

    def __init__(self, nu: float = 0.05, seed: int = 0):  # deterministic; seed kept for a uniform signature
        self.model = make_pipeline(StandardScaler(), OneClassSVM(nu=nu, gamma="scale"))

    def fit(self, x, y=None):
        self.model.fit(x)
        return self

    def score(self, x):
        return -self.model.decision_function(x)


class AutoencoderDetector(Detector):
    """A neural autoencoder: three hidden layers squeeze each window through a
    narrow bottleneck and rebuild it. Trained on history, it rebuilds normal
    behaviour well; the reconstruction error is the anomaly score."""

    name = "autoencoder"

    def __init__(self, hidden: tuple[int, ...] = (32, 6, 32), seed: int = 0, max_iter: int = 2000):
        self.scaler = StandardScaler()
        self.net = MLPRegressor(
            hidden_layer_sizes=hidden, activation="relu", solver="adam", alpha=1e-3,
            learning_rate_init=1e-3, max_iter=max_iter, early_stopping=True, validation_fraction=0.15,
            n_iter_no_change=30, random_state=seed,
        )

    def fit(self, x, y=None):
        z = self.scaler.fit_transform(x)
        self.net.fit(z, z)
        return self

    def score(self, x):
        z = self.scaler.transform(x)
        return np.mean((self.net.predict(z) - z) ** 2, axis=1)


class GradientBoostingDetector(Detector):
    name = "gradient_boosting"
    supervised = True

    def __init__(self, seed: int = 0):
        self.model = HistGradientBoostingClassifier(
            max_iter=300, learning_rate=0.05, class_weight="balanced", random_state=seed,
        )

    def fit(self, x, y=None):
        self.model.fit(x, y)
        return self

    def score(self, x):
        return self.model.predict_proba(x)[:, 1]


class RandomForestDetector(Detector):
    name = "random_forest"
    supervised = True

    def __init__(self, seed: int = 0):
        self.model = RandomForestClassifier(
            n_estimators=400, min_samples_leaf=2, class_weight="balanced_subsample", random_state=seed, n_jobs=-1,
        )

    def fit(self, x, y=None):
        self.model.fit(x, y)
        return self

    def score(self, x):
        return self.model.predict_proba(x)[:, 1]


class NeuralClassifierDetector(Detector):
    """A small feed-forward neural network classifier (two hidden layers)."""

    name = "neural_classifier"
    supervised = True

    def __init__(self, seed: int = 0):
        self.model = make_pipeline(StandardScaler(), MLPClassifier(
            hidden_layer_sizes=(64, 32), alpha=1e-3, max_iter=2000, early_stopping=True,
            n_iter_no_change=30, random_state=seed,
        ))

    def fit(self, x, y=None):
        self.model.fit(x, y)
        return self

    def score(self, x):
        return self.model.predict_proba(x)[:, 1]


class EnsembleDetector(Detector):
    """Averages member detectors after putting each on a common scale (its score's
    percentile among the training windows). Pairing a supervised member with an
    unsupervised one keeps some sensitivity to attack kinds absent from the labels."""

    name = "ensemble"
    supervised = True

    def __init__(self, members: tuple[str, ...] = ("gradient_boosting", "autoencoder"), seed: int = 0):
        self.members = [DETECTORS[m](seed=seed) for m in members]
        self.reference: list[np.ndarray] = []

    def fit(self, x, y=None):
        self.reference = []
        for member in self.members:
            member.fit(x, y if member.supervised else None)
            self.reference.append(np.sort(member.score(x)))
        return self

    def score(self, x):
        ranks = [np.searchsorted(ref, m.score(x), side="right") / len(ref) for m, ref in zip(self.members, self.reference, strict=True)]
        return np.mean(ranks, axis=0)


DETECTORS: dict[str, type[Detector]] = {
    d.name: d for d in (
        IsolationForestDetector, LocalOutlierDetector, OneClassSVMDetector, AutoencoderDetector,
        GradientBoostingDetector, RandomForestDetector, NeuralClassifierDetector,
    )
}
DETECTORS[EnsembleDetector.name] = EnsembleDetector
