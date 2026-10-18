from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

import joblib
import numpy as np
import pandas as pd
from sklearn.ensemble import HistGradientBoostingClassifier, HistGradientBoostingRegressor

from . import metrics

MODEL_FILE = "redispatch_model_{direction}.joblib"


@dataclass(frozen=True)
class Settings:
    test_days: int
    threshold: float
    learning_rate: float
    max_iter: int
    max_leaf_nodes: int
    random_state: int
    negative_fraction: float = 1.0
    recency_half_life_days: float = 0.0
    l2_regularization: float = 0.0
    min_samples_leaf: int = 20


def _estimator(settings: Settings, categorical: list[str], features: list[str]) -> HistGradientBoostingClassifier:
    """Build an untrained classifier from the model settings."""
    mask = [name in categorical for name in features] if categorical else None
    return HistGradientBoostingClassifier(
        learning_rate=settings.learning_rate,
        max_iter=settings.max_iter,
        max_leaf_nodes=settings.max_leaf_nodes,
        l2_regularization=settings.l2_regularization,
        min_samples_leaf=settings.min_samples_leaf,
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


def _recency(dates: pd.Series, end: pd.Timestamp, half_life_days: float) -> np.ndarray:
    """Halve a row's weight for every half-life it lies before the last training day."""
    if not half_life_days:
        return np.ones(len(dates))
    return np.power(0.5, (end - dates).dt.days.to_numpy() / half_life_days)


def train(
    dataset: pd.DataFrame,
    features: list[str],
    settings: Settings,
    categorical: list[str] | None = None,
    extra: dict | None = None,
) -> tuple[dict, dict]:
    """Fit on history, score the most recent days, then refit on all labels."""
    categorical = categorical or []
    known = dataset["target"].notna()
    labelled = dataset if known.all() else dataset[known].reset_index(drop=True)
    if labelled.empty:
        raise ValueError("No labelled rows available for training")

    cutoff = labelled["delivery_date"].max() - pd.Timedelta(days=settings.test_days)
    fit_part = labelled[labelled["delivery_date"] <= cutoff]
    test_part = labelled[labelled["delivery_date"] > cutoff]

    report = {"train_rows": int(len(fit_part)), "test_rows": int(len(test_part)), "negative_fraction": settings.negative_fraction}
    presampled = "weight" in labelled
    if len(fit_part) and len(test_part):
        if presampled:
            rows, weights = fit_part, fit_part["weight"].to_numpy()
        else:
            rows, weights = _sample(fit_part, settings.negative_fraction, settings.random_state)
        weights = weights * _recency(rows["delivery_date"], cutoff, settings.recency_half_life_days)
        holdout = _estimator(settings, categorical, features).fit(rows[features], rows["target"], sample_weight=weights)
        probability = holdout.predict_proba(test_part[features])[:, 1]
        report["holdout"] = metrics.score(test_part["target"], probability, settings.threshold)
        if "target_mwh" in test_part:
            size = _size_estimator(settings, categorical, features, rows, weights)
            expected = probability * size.predict(test_part[features])
            scored = test_part[["delivery_date", "period", "unit"]].assign(mwh=test_part["target_mwh"].to_numpy(), expected_mwh=expected)
            report["holdout_energy"] = metrics.energy(scored)

    if presampled:
        # the holdout days were kept whole, so only they are sampled here
        late, late_weights = _sample(test_part, settings.negative_fraction, settings.random_state)
        rows = pd.concat([fit_part, late], ignore_index=True)
        weights = np.concatenate([fit_part["weight"].to_numpy(), late_weights])
    else:
        rows, weights = _sample(labelled, settings.negative_fraction, settings.random_state)
    weights = weights * _recency(rows["delivery_date"], labelled["delivery_date"].max(), settings.recency_half_life_days)
    estimator = _estimator(settings, categorical, features).fit(rows[features], rows["target"], sample_weight=weights)
    trained_at = datetime.now(timezone.utc)
    bundle = {
        "estimator": estimator,
        "size_estimator": _size_estimator(settings, categorical, features, rows, weights) if "target_mwh" in rows else None,
        "features": list(features),
        "threshold": settings.threshold,
        "model_version": f"hgb-{trained_at:%Y%m%dT%H%M%SZ}",
        "trained_at": trained_at.isoformat(),
        **(extra or {}),
    }
    report["model_version"] = bundle["model_version"]
    return bundle, report


def _size_estimator(settings: Settings, categorical: list[str], features: list[str], rows: pd.DataFrame, weights: np.ndarray):
    """Fit a Poisson regressor for the MWh redispatched in rows that reach the label threshold."""
    positive = (rows["target"] == 1).to_numpy()
    mask = [name in categorical for name in features] if categorical else None
    return HistGradientBoostingRegressor(
        loss="poisson",
        learning_rate=settings.learning_rate,
        max_iter=settings.max_iter,
        max_leaf_nodes=settings.max_leaf_nodes,
        l2_regularization=settings.l2_regularization,
        min_samples_leaf=settings.min_samples_leaf,
        random_state=settings.random_state,
        categorical_features=mask,
    ).fit(rows.loc[positive, features], rows.loc[positive, "target_mwh"], sample_weight=weights[positive])


def model_path(model_dir: Path, direction: str) -> Path:
    """Return the model file for one redispatch direction."""
    return model_dir / MODEL_FILE.format(direction=direction.lower())


def save(bundle: dict, model_dir: Path) -> Path:
    """Write the model bundle to the model directory."""
    model_dir.mkdir(parents=True, exist_ok=True)
    path = model_path(model_dir, bundle["direction"])
    joblib.dump(bundle, path)
    return path


def load(model_dir: Path, direction: str) -> dict:
    """Read one direction's model bundle from the model directory."""
    path = model_path(model_dir, direction)
    if not path.exists():
        raise FileNotFoundError(f"No trained model at {path}; run the train command first")
    return joblib.load(path)


def predict(bundle: dict, dataset: pd.DataFrame) -> np.ndarray:
    """Return redispatch probabilities for each feature row."""
    return bundle["estimator"].predict_proba(dataset[bundle["features"]])[:, 1]


def predict_mwh(bundle: dict, dataset: pd.DataFrame, probability: np.ndarray) -> np.ndarray:
    """Return expected MWh per row: the redispatch probability times the size model's MWh."""
    if bundle.get("size_estimator") is None:
        return np.full(len(dataset), np.nan)
    return probability * bundle["size_estimator"].predict(dataset[bundle["features"]])
