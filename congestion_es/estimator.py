from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

import joblib
import numpy as np
import pandas as pd
from lightgbm import LGBMClassifier

from . import metrics

MODEL_FILE = "redispatch_model_{direction}.joblib"
HOLDOUT_COLUMNS = ["delivery_date", "period", "unit"]


@dataclass(frozen=True)
class Settings:
    test_days: int
    threshold: float
    learning_rate: float
    n_estimators: int
    num_leaves: int
    random_state: int
    negative_fraction: float = 1.0
    recency_half_life_days: float = 0.0
    reg_lambda: float = 0.0
    min_child_samples: int = 20
    max_cat_threshold: int = 32
    cat_smooth: float = 10.0
    cat_l2: float = 10.0
    min_data_per_group: int = 100


def _estimator(settings: Settings) -> LGBMClassifier:
    """Build an untrained yes/no classifier from the model settings."""
    return LGBMClassifier(
        learning_rate=settings.learning_rate,
        n_estimators=settings.n_estimators,
        num_leaves=settings.num_leaves,
        reg_lambda=settings.reg_lambda,
        min_child_samples=settings.min_child_samples,
        max_cat_threshold=settings.max_cat_threshold,
        cat_smooth=settings.cat_smooth,
        cat_l2=settings.cat_l2,
        min_data_per_group=settings.min_data_per_group,
        random_state=settings.random_state,
        verbose=-1,
    )


def _fit(settings: Settings, categorical: list[str], features: list[str], rows: pd.DataFrame, weights: np.ndarray) -> LGBMClassifier:
    """Fit one classifier with the given categorical inputs."""
    return _estimator(settings).fit(rows[features], rows["target"].astype(int), sample_weight=weights, categorical_feature=categorical or "auto")


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
) -> tuple[dict, dict, pd.DataFrame | None]:
    """Fit on history, score the most recent days, then refit on all labels; also return the scored holdout rows."""
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
    holdout = None
    if len(fit_part) and len(test_part):
        if presampled:
            rows, weights = fit_part, fit_part["weight"].to_numpy()
        else:
            rows, weights = _sample(fit_part, settings.negative_fraction, settings.random_state)
        weights = weights * _recency(rows["delivery_date"], cutoff, settings.recency_half_life_days)
        model = _fit(settings, categorical, features, rows, weights)
        probability = model.predict_proba(test_part[features])[:, 1]
        report["holdout"] = metrics.score(test_part["target"], probability, settings.threshold)
        holdout = test_part[HOLDOUT_COLUMNS].assign(target=test_part["target"].to_numpy(), probability=probability).reset_index(drop=True)

    if presampled:
        # the holdout days were kept whole, so only they are sampled here
        late, late_weights = _sample(test_part, settings.negative_fraction, settings.random_state)
        rows = pd.concat([fit_part, late], ignore_index=True)
        weights = np.concatenate([fit_part["weight"].to_numpy(), late_weights])
    else:
        rows, weights = _sample(labelled, settings.negative_fraction, settings.random_state)
    weights = weights * _recency(rows["delivery_date"], labelled["delivery_date"].max(), settings.recency_half_life_days)
    estimator = _fit(settings, categorical, features, rows, weights)
    trained_at = datetime.now(timezone.utc)
    bundle = {
        "estimator": estimator,
        "features": list(features),
        "threshold": settings.threshold,
        "model_version": f"lgbm-{trained_at:%Y%m%dT%H%M%SZ}",
        "trained_at": trained_at.isoformat(),
        **(extra or {}),
    }
    report["model_version"] = bundle["model_version"]
    return bundle, report, holdout


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
