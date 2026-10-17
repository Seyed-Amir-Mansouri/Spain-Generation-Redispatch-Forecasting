from __future__ import annotations

import numpy as np
import pandas as pd
from sklearn.metrics import brier_score_loss, roc_auc_score


def score(y_true: np.ndarray, probability: np.ndarray, threshold: float) -> dict:
    """Return Brier, skill against climatology, AUC and flag precision and recall."""
    y_true = np.asarray(y_true, dtype=int)
    probability = np.asarray(probability, dtype=float)
    flag = probability >= threshold
    positive_rate = y_true.mean() if len(y_true) else np.nan

    brier = brier_score_loss(y_true, probability) if len(y_true) else np.nan
    climatology = positive_rate * (1 - positive_rate) if len(y_true) else np.nan
    skill = 1 - brier / climatology if climatology and climatology > 0 else np.nan

    has_both = len(np.unique(y_true)) == 2
    auc = roc_auc_score(y_true, probability) if has_both else np.nan

    true_positive = np.sum(flag & (y_true == 1))
    predicted = flag.sum()
    actual = y_true.sum()
    precision = true_positive / predicted if predicted else np.nan
    recall = true_positive / actual if actual else np.nan

    return {
        "n": int(len(y_true)),
        "positive_rate": float(positive_rate),
        "brier": float(brier),
        "skill": float(skill),
        "roc_auc": float(auc),
        "precision": float(precision),
        "recall": float(recall),
    }


def _corr(a, b) -> float:
    """Return the Pearson correlation, or NaN when either side is constant."""
    a, b = np.asarray(a, dtype=float), np.asarray(b, dtype=float)
    return float(np.corrcoef(a, b)[0, 1]) if len(a) > 1 and a.std() > 0 and b.std() > 0 else float("nan")


def energy(frame: pd.DataFrame) -> dict:
    """Score expected MWh against actual MWh per cell and quarter-hour, per cell and day, per cell and nationally."""
    day = frame.groupby(["delivery_date", "area"])[["mwh", "expected_mwh"]].sum()
    cell = frame.groupby("area")[["mwh", "expected_mwh"]].sum()
    national = frame.groupby(["delivery_date", "period"])[["mwh", "expected_mwh"]].sum()
    actual = frame["mwh"].sum()
    return {
        "actual_gwh": float(actual / 1000),
        "predicted_to_actual": float(frame["expected_mwh"].sum() / actual) if actual else float("nan"),
        "mae_mwh": float((frame["expected_mwh"] - frame["mwh"]).abs().mean()),
        "corr_cell_quarter": _corr(frame["expected_mwh"], frame["mwh"]),
        "corr_cell_day": _corr(day["expected_mwh"], day["mwh"]),
        "wape_cell_day": float((day["expected_mwh"] - day["mwh"]).abs().sum() / day["mwh"].sum()) if actual else float("nan"),
        "corr_cell_total": _corr(cell["expected_mwh"], cell["mwh"]),
        "corr_national_quarter": _corr(national["expected_mwh"], national["mwh"]),
    }
