from __future__ import annotations

import pandas as pd

MARKET_KEY = ["delivery_date", "period"]


def add_period(frame: pd.DataFrame, stamps: pd.Series) -> pd.DataFrame:
    """Add the local delivery date and quarter of the day (0-95) of tz-aware local timestamps."""
    frame["delivery_date"] = stamps.dt.normalize().dt.tz_localize(None).to_numpy()
    frame["period"] = (stamps.dt.hour * 4 + stamps.dt.minute // 15).to_numpy()
    return frame


def quarter_mean(series: pd.Series, column: str, end: pd.Timestamp) -> pd.DataFrame:
    """Resample a tz-aware series to local quarter-hour means, excluding the end boundary."""
    series = series[series.index < end]
    quarters = series.resample("15min").mean()
    frame = quarters.rename(column).reset_index()
    stamps = frame[frame.columns[0]]
    frame = add_period(frame, stamps)
    return frame.groupby(MARKET_KEY, as_index=False)[column].mean()
