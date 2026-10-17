from __future__ import annotations

import pandas as pd

from . import ledger, metrics
from .config import Config
from .features import KEY, label_rows
from .pipeline import Inputs


def evaluate(config: Config, predictions: pd.DataFrame, inputs: Inputs, label_end: pd.Timestamp, as_of: pd.Timestamp) -> dict:
    """Compare the latest prediction per key of the config's direction with the attributed redispatch."""
    direction = config.labels.direction
    latest = ledger.latest_predictions(predictions[predictions["direction"] == direction], label_end)
    latest["congested"] = label_rows(latest[KEY], inputs.attributed, inputs.label_days, config.labels.min_mwh).to_numpy()
    labelled = latest[latest["congested"].notna()].copy()
    result = ledger.evaluate(
        labelled,
        label_end,
        as_of,
        config.validation.window_days,
        config.model.threshold,
        config.validation.min_skill,
    )
    labelled["mwh"] = labelled[KEY].merge(inputs.attributed, on=KEY, how="left")["mwh"].fillna(0.0).to_numpy()
    energy = metrics.energy(labelled) if len(labelled) and labelled["mwh"].sum() > 0 else {}
    return {"direction": direction, **result, **{f"overall_{key}": value for key, value in energy.items()}}
