from __future__ import annotations

from pathlib import Path

import pandas as pd

from . import metrics

PREDICTION_COLUMNS = ["delivery_date", "period", "unit", "direction", "issued_at", "model_version", "risk_probability", "risk_flag", "expected_mwh"]
KEY = ["delivery_date", "period", "unit", "direction"]


def append(predictions: pd.DataFrame, path: Path) -> None:
    """Append predictions to the ledger CSV, writing the header only once."""
    path.parent.mkdir(parents=True, exist_ok=True)
    predictions.to_csv(path, mode="a", header=not path.exists(), index=False)


def load(path: Path) -> pd.DataFrame:
    """Read the prediction ledger with parsed dates and issue times."""
    if not path.exists():
        raise FileNotFoundError(f"No prediction ledger at {path}; run the predict command first")
    ledger = pd.read_csv(path)
    ledger["delivery_date"] = pd.to_datetime(ledger["delivery_date"]).dt.normalize()
    ledger["unit"] = ledger["unit"].astype(str)
    ledger["issued_at"] = pd.to_datetime(ledger["issued_at"], utc=True)
    return ledger


def latest_predictions(ledger: pd.DataFrame, label_end: pd.Timestamp) -> pd.DataFrame:
    """Keep the most recently issued prediction per key for settled delivery dates."""
    settled = ledger[ledger["delivery_date"] <= label_end]
    ordered = settled.sort_values("issued_at")
    return ordered.drop_duplicates(subset=KEY, keep="last").reset_index(drop=True)


def evaluate(
    labelled: pd.DataFrame,
    label_end: pd.Timestamp,
    as_of: pd.Timestamp,
    window_days: int,
    threshold: float,
    min_skill: float,
) -> dict:
    """Score labelled predictions overall and over a recent window, and check the skill alert."""
    window_start = label_end - pd.Timedelta(days=window_days - 1)
    recent = labelled[labelled["delivery_date"] >= window_start]

    overall = metrics.score(labelled["redispatched"], labelled["risk_probability"], threshold)
    window = metrics.score(recent["redispatched"], recent["risk_probability"], threshold)

    skill = window["skill"]
    alert = bool(pd.notna(skill) and skill < min_skill)
    return {
        "as_of": as_of.isoformat(),
        "label_end": label_end.date().isoformat(),
        "window_start": window_start.date().isoformat(),
        "model_versions": ";".join(sorted(labelled["model_version"].unique())),
        **{f"window_{key}": value for key, value in window.items()},
        **{f"overall_{key}": value for key, value in overall.items()},
        "alert": alert,
    }


def append_history(result: dict, path: Path) -> pd.DataFrame:
    """Append one validation row to the history CSV and return the full history."""
    path.parent.mkdir(parents=True, exist_ok=True)
    pd.DataFrame([result]).to_csv(path, mode="a", header=not path.exists(), index=False)
    return pd.read_csv(path)
