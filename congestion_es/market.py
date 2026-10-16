from __future__ import annotations

from pathlib import Path

import pandas as pd

from .timeseries import MARKET_KEY, quarter_mean

AREA = "ES"
TIMEZONE = "Europe/Madrid"
ENTSOE_COLUMNS = ["da_price", "load_forecast_mw", "wind_forecast_mw", "solar_forecast_mw"]


def build_client(api_key: str):
    """Construct an ENTSO-E pandas client from an API key."""
    from entsoe import EntsoePandasClient

    return EntsoePandasClient(api_key=api_key)


def months(start: pd.Timestamp, end: pd.Timestamp) -> list[tuple[pd.Timestamp, pd.Timestamp]]:
    """Split a date range into calendar-month chunks with an exclusive end."""
    edges = pd.date_range(start.to_period("M").to_timestamp(), end, freq="MS").tolist()
    chunks = []
    for first in edges:
        last = first + pd.offsets.MonthBegin(1)
        chunks.append((max(first, start), min(last, end)))
    return [(a, b) for a, b in chunks if a < b]


def _safe(query, *args, **kwargs):
    """Run an ENTSO-E query, returning None when the platform has no data for the range."""
    from entsoe.exceptions import NoMatchingDataError

    try:
        return query(*args, **kwargs)
    except NoMatchingDataError:
        return None


def fetch_market(client, start: pd.Timestamp, end: pd.Timestamp) -> pd.DataFrame:
    """Fetch quarter-hour day-ahead prices and load, wind and solar forecasts for Spain from ENTSO-E."""
    frames = []
    for first, last in months(start, end):
        a, b = first.tz_localize(TIMEZONE), last.tz_localize(TIMEZONE)
        series = {"da_price": _safe(client.query_day_ahead_prices, AREA, start=a, end=b)}
        load = _safe(client.query_load_forecast, AREA, start=a, end=b)
        if load is not None:
            series["load_forecast_mw"] = load.iloc[:, 0]
        renewables = _safe(client.query_wind_and_solar_forecast, AREA, start=a, end=b)
        if renewables is not None:
            wind = [renewables[c] for c in ("Wind Onshore", "Wind Offshore") if c in renewables]
            if wind:
                series["wind_forecast_mw"] = sum(wind)
            if "Solar" in renewables:
                series["solar_forecast_mw"] = renewables["Solar"]
        parts = [quarter_mean(values, name, b) for name, values in series.items() if values is not None]
        if not parts:
            continue
        month = parts[0]
        for part in parts[1:]:
            month = month.merge(part, on=MARKET_KEY, how="outer")
        frames.append(month)
    frame = pd.concat(frames, ignore_index=True) if frames else pd.DataFrame(columns=MARKET_KEY)
    return frame.reindex(columns=MARKET_KEY + ENTSOE_COLUMNS)


def fetch_constraints(client, indicators: dict[str, int], start: pd.Timestamp, end: pd.Timestamp) -> pd.DataFrame:
    """Fetch REE's national day-ahead constraint energy per quarter-hour for each configured indicator."""
    days = pd.date_range(start, end - pd.Timedelta(days=1), freq="D")
    frame = pd.MultiIndex.from_product([days, range(96)], names=MARKET_KEY).to_frame(index=False)
    for name, indicator_id in indicators.items():
        parts = [client.indicator(indicator_id, first.date(), (last - pd.Timedelta(days=1)).date()) for first, last in months(start, end)]
        values = pd.concat(parts, ignore_index=True).rename(columns={"value": name})
        values["delivery_date"] = pd.to_datetime(values["delivery_date"])
        values["period"] = values["period"].astype(int)
        values[name] = values[name].abs()
        frame = frame.merge(values, on=MARKET_KEY, how="left")
        frame[name] = frame[name].fillna(0.0)
    return frame


def upsert(path: Path, frame: pd.DataFrame, key: list[str] = MARKET_KEY) -> pd.DataFrame:
    """Merge new values into a CSV column by column, keeping other columns of existing rows."""
    frame = frame.assign(delivery_date=pd.to_datetime(frame["delivery_date"]), period=frame["period"].astype(int))
    values = [column for column in frame.columns if column not in key]
    frame[values] = frame[values].apply(pd.to_numeric, errors="coerce")
    if path.exists():
        existing = pd.read_csv(path, parse_dates=["delivery_date"]).set_index(key)
        new = frame.set_index(key)
        combined = existing.reindex(existing.index.union(new.index))
        for column in values:
            update = new[column].reindex(combined.index)
            combined[column] = update.combine_first(combined[column]) if column in combined else update
        frame = combined.reset_index()
    frame = frame.sort_values(key).reset_index(drop=True)
    path.parent.mkdir(parents=True, exist_ok=True)
    frame.to_csv(path, index=False)
    return frame
