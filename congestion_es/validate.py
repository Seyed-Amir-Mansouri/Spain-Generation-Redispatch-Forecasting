from __future__ import annotations

import pandas as pd

from . import ledger
from .config import Config
from .features import KEY, label_rows
from .pipeline import Inputs


def evaluate(config: Config, predictions: pd.DataFrame, inputs: Inputs, label_end: pd.Timestamp, as_of: pd.Timestamp) -> dict:
    """Compare the latest prediction per key with the attributed redispatch labels."""
    latest = ledger.latest_predictions(predictions, label_end)
    latest["congested"] = label_rows(latest[KEY], inputs.attributed, inputs.label_days, config.labels.min_mwh).to_numpy()
    labelled = latest[latest["congested"].notna()]
    return ledger.evaluate(
        labelled,
        label_end,
        as_of,
        config.validation.window_days,
        config.model.threshold,
        config.validation.min_skill,
    )
