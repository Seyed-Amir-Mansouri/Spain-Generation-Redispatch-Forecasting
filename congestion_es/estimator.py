from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

import joblib
import numpy as np
import pandas as pd
from sklearn.ensemble import HistGradientBoostingClassifier

from . import metrics

MODEL_FILE = "congestion_model.joblib"


@dataclass(frozen=True)
class Settings:
    test_days: int
    threshold: float
    learning_rate: float
    max_iter: int
    max_leaf_nodes: int
    random_state: int
    negative_fraction: float = 1.0


def _estimator(settings: Settings, categorical: list[str], features: list[str]) -> HistGradientBoostingClassifier:
    """Build an untrained classifier from the model settings."""
    mask = [name in categorical for name in features] if categorical else None
    return HistGradientBoostingClassifier(
        learning_rate=settings.learning_rate,
        max_iter=settings.max_iter,
        max_leaf_nodes=settings.max_leaf_nodes,
        random_state=settings.random_state,
        categorical_features=mask,
    )


def _sample(frame: pd.DataFrame, fraction: float, seed: int) -> tuple[pd.DataFrame, np.ndarray]:
    """Keep every positive row and a random share of negatives, weighting the kept negatives back up."""
    if fraction >= 1:
        return frame, np.ones(len(frame))
    negatives = frame["target"] == 0
    keep = ~negatives | (np.random.default_rng(seed).random(len(frame)) < fraction)
    kept = frame[keep]
    return kept, np.where(kept["target"] == 0, 1 / fraction, 1.0)


def train(
    dataset: pd.DataFrame,
    features: list[str],
    settings: Settings,
    categorical: list[str] | None = None,
    extra: dict | None = None,
) -> tuple[dict, dict]:
    """Fit on history, score the most recent days, then refit on all labels."""
    categorical = categorical or []
    labelled = dataset[dataset["target"].notna()].reset_index(drop=True)
    if labelled.empty:
        raise ValueError("No labelled rows available for training")

    cutoff = labelled["delivery_date"].max() - pd.Timedelta(days=settings.test_days)
    fit_part = labelled[labelled["delivery_date"] <= cutoff]
    test_part = labelled[labelled["delivery_date"] > cutoff]

    report = {"train_rows": int(len(fit_part)), "test_rows": int(len(test_part)), "negative_fraction": settings.negative_fraction}
    if len(fit_part) and len(test_part):
        rows, weights = _sample(fit_part, settings.negative_fraction, settings.random_state)
        holdout = _estimator(settings, categorical, features).fit(rows[features], rows["target"], sample_weight=weights)
        probability = holdout.predict_proba(test_part[features])[:, 1]
        report["holdout"] = metrics.score(test_part["target"], probability, settings.threshold)

    rows, weights = _sample(labelled, settings.negative_fraction, settings.random_state)
    estimator = _estimator(settings, categorical, features).fit(rows[features], rows["target"], sample_weight=weights)
    trained_at = datetime.now(timezone.utc)
    bundle = {
        "estimator": estimator,
        "features": list(features),
        "threshold": settings.threshold,
        "model_version": f"hgb-{trained_at:%Y%m%dT%H%M%SZ}",
        "trained_at": trained_at.isoformat(),
        **(extra or {}),
    }
    report["model_version"] = bundle["model_version"]
    return bundle, report


def save(bundle: dict, model_dir: Path) -> Path:
    """Write the model bundle to the model directory."""
    model_dir.mkdir(parents=True, exist_ok=True)
    path = model_dir / MODEL_FILE
    joblib.dump(bundle, path)
    return path


def load(model_dir: Path) -> dict:
    """Read the model bundle from the model directory."""
    path = model_dir / MODEL_FILE
    if not path.exists():
        raise FileNotFoundError(f"No trained model at {path}; run the train command first")
    return joblib.load(path)


def predict(bundle: dict, dataset: pd.DataFrame) -> np.ndarray:
    """Return congestion probabilities for each feature row."""
    return bundle["estimator"].predict_proba(dataset[bundle["features"]])[:, 1]
