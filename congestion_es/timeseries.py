from __future__ import annotations

import pandas as pd

MARKET_KEY = ["delivery_date", "hour"]


def hourly_mean(series: pd.Series, column: str, end: pd.Timestamp) -> pd.DataFrame:
    """Resample a tz-aware series to local hourly means, excluding the end boundary."""
    series = series[series.index < end]
    hourly = series.resample("1h").mean()
    frame = hourly.rename(column).reset_index()
    frame = frame.rename(columns={frame.columns[0]: "timestamp"})
    frame["delivery_date"] = frame["timestamp"].dt.normalize().dt.tz_localize(None)
    frame["hour"] = frame["timestamp"].dt.hour
    return frame[MARKET_KEY + [column]]
