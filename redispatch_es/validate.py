from __future__ import annotations

import numpy as np
import pandas as pd

from . import ledger
from .config import Config
from .features import KEY, label_rows
from .pipeline import DIRECTION_NAMES, Inputs


def evaluate(config: Config, predictions: pd.DataFrame, inputs: dict[str, Inputs], label_end: pd.Timestamp, as_of: pd.Timestamp) -> dict:
    """Compare the latest up/down/nothing call per unit-quarter with the per-unit redispatch REE has published."""
    latest = ledger.latest_predictions(predictions, label_end)
    labels = {
        name: label_rows(latest[KEY], inputs[direction].redispatch, inputs[direction].label_days, config.labels.min_mwh).to_numpy()
        for direction, name in DIRECTION_NAMES.items()
    }
    known = ~np.isnan(labels["down"]) & ~np.isnan(labels["up"])
    latest["actual"] = np.select([labels["up"] == 1, labels["down"] == 1], ["up", "down"], "nothing")
    return ledger.evaluate(
        latest[known].copy(),
        label_end,
        as_of,
        config.validation.window_days,
        config.model.threshold,
        config.validation.min_skill,
    )
