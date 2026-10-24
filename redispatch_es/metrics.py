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


def result_tables(rows: dict[str, pd.DataFrame], title: str) -> str:
    """Draw one table per direction with each row's happened, spotted and correct calls and its wrong calls split into missed and not real."""
    width = max(len(label) for label in rows) + 2
    lines = []
    for direction in ("down", "up"):
        lines += [
            f"{direction.upper()} ({title})",
            "┌" + "─" * width + "┬──────────┬─────────┬─────────┬──────────────────────────────┐",
            "│" + " " * width + "│          │         │         │            Wrong             │",
            "│ " + "Version".ljust(width - 1) + "│ Happened │ Spotted │ Correct ├─────────┬─────────┬──────────┤",
            "│" + " " * width + "│          │         │         │  Total  │ Missed  │ Not real │",
            "├" + "─" * width + "┼──────────┼─────────┼─────────┼─────────┼─────────┼──────────┤",
        ]
        for label, frame in rows.items():
            counts = decision_counts(frame)
            happened, spotted, correct = counts[f"{direction}_happened"], counts[f"{direction}_said"], counts[f"{direction}_right"]
            values = [happened, spotted, correct, happened + spotted - 2 * correct, happened - correct, spotted - correct]
            cells = "│".join(f"{value:>{size - 1},} " for value, size in zip(values, (10, 9, 9, 9, 9, 10)))
            lines.append("│ " + label.ljust(width - 1) + "│" + cells + "│")
        lines += ["└" + "─" * width + "┴──────────┴─────────┴─────────┴─────────┴─────────┴──────────┘", ""]
    return "\n".join(lines)


def decide(down_probability, up_probability, threshold: float) -> np.ndarray:
    """Call each row up, down or nothing: the more likely direction when its probability reaches the threshold."""
    down, up = np.asarray(down_probability, dtype=float), np.asarray(up_probability, dtype=float)
    return np.where(np.maximum(down, up) >= threshold, np.where(up > down, "up", "down"), "nothing")


def decision_counts(frame: pd.DataFrame) -> dict:
    """Count right, missed, wrong-direction and false-alarm calls per unit-quarter, and per quarter-hour for each direction."""
    actual, decision = frame["actual"].to_numpy(), frame["decision"].to_numpy()
    result = {"n": int(len(frame)), "correct": int((actual == decision).sum())}
    result["accuracy"] = result["correct"] / result["n"] if result["n"] else float("nan")
    for name, other in (("down", "up"), ("up", "down")):
        happened, said = actual == name, decision == name
        right = int((happened & said).sum())
        quarters = frame.assign(happened=happened, said=said, right=happened & said).groupby(["delivery_date", "period"])[["happened", "said", "right"]].any()
        result.update({
            f"{name}_happened": int(happened.sum()),
            f"{name}_said": int(said.sum()),
            f"{name}_right": right,
            f"{name}_missed": int((happened & (decision == "nothing")).sum()),
            f"{name}_wrong_direction": int((happened & (decision == other)).sum()),
            f"{name}_false_alarms": int(said.sum()) - right,
            f"{name}_precision": right / said.sum() if said.any() else float("nan"),
            f"{name}_recall": right / happened.sum() if happened.any() else float("nan"),
            f"{name}_quarters_happened": int(quarters["happened"].sum()),
            f"{name}_quarters_said": int(quarters["said"].sum()),
            f"{name}_quarters_missed": int((quarters["happened"] & ~quarters["said"]).sum()),
            f"{name}_quarters_false_alarm": int((quarters["said"] & ~quarters["happened"]).sum()),
            f"{name}_quarters_one_unit_right": int(quarters["right"].sum()),
        })
    return result
