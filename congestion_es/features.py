from __future__ import annotations

import numpy as np
import pandas as pd

from .config import Config
from .timeseries import MARKET_KEY

KEY = MARKET_KEY + ["unit"]
CATEGORICAL = ["unit_id", "technology_id"]
UNIT_COLUMNS = ["unit_id", "technology_id"]

# readable model names for the source columns
SOURCE_NAMES = {
    "da_price": "day_ahead_price",
    "ida1_price": "intraday1_price",
    "ida2_price": "intraday2_price",
    "ida3_price": "intraday3_price",
    "load_forecast_mw": "demand_forecast_mw",
    "wind_forecast_mw": "wind_generation_forecast_mw",
    "solar_forecast_mw": "solar_generation_forecast_mw",
    "wind_speed_100m": "area_wind_speed_kmh",
    "shortwave_radiation": "area_solar_radiation_wm2",
    "cloud_cover": "area_cloud_cover_pct",
}
WEATHER_COLUMNS = ["area_wind_speed_kmh", "area_solar_radiation_wm2", "area_cloud_cover_pct"]


def spain_redispatch_name(indicator: str) -> str:
    """Name an REE Phase I indicator as Spain's total redispatch, e.g. spain_redispatch_down_overload_normal_mwh."""
    name = indicator.replace("phase1_", "").replace("overload_base", "overload_normal")
    return f"spain_redispatch_{name}_mwh"


def feature_columns(config: Config) -> list[str]:
    """List the model inputs, which depend on the configured indicators and IDA3 usage."""
    auctions = (1, 2, 3) if config.features.use_ida3 else (1, 2)
    return (
        UNIT_COLUMNS
        + ["quarter_hour", "day_of_week", "month", "is_weekend", "is_hourly", "day_ahead_price"]
        + [f"intraday{k}_price" for k in auctions]
        + [f"intraday{k}_minus_day_ahead_price" for k in auctions]
        + ["demand_forecast_mw", "wind_generation_forecast_mw", "solar_generation_forecast_mw", "demand_minus_wind_solar_mw"]
        + [spain_redispatch_name(name) for name in config.constraint_indicators]
        + WEATHER_COLUMNS
    )


def add_weather(frame: pd.DataFrame, weather: pd.DataFrame, points: pd.DataFrame) -> pd.DataFrame:
    """Attach the weather forecast of each unit's area."""
    weather = weather.merge(points, on="point", how="inner").drop(columns="point").rename(columns=SOURCE_NAMES)
    return frame.merge(weather, on=KEY, how="left")


def label_rows(keys: pd.DataFrame, redispatch: pd.DataFrame, label_days: set, min_mwh: float) -> pd.Series:
    """Flag keys whose redispatch reaches the threshold; days without a file stay unknown."""
    merged = keys[KEY].merge(redispatch, on=KEY, how="left")
    redispatched = (merged["mwh"].fillna(0.0) >= min_mwh).astype(float)
    known = merged["delivery_date"].isin(label_days)
    return redispatched.where(known.to_numpy(), np.nan)


def redispatch_labels(config: Config, keys: pd.DataFrame, redispatch: pd.DataFrame, label_days: set) -> pd.DataFrame:
    """Give every key its redispatched flag and MWh, unknown on days without published redispatch."""
    frame = keys.sort_values(KEY).reset_index(drop=True)
    frame["redispatched"] = label_rows(frame, redispatch, label_days, config.labels.min_mwh).to_numpy().astype("float32")
    mwh = frame[KEY].merge(redispatch[KEY + ["mwh"]], on=KEY, how="left")["mwh"].fillna(0.0).clip(lower=0.0)
    frame["redispatch_mwh"] = mwh.where(frame["redispatched"].notna().to_numpy()).to_numpy().astype("float32")
    return frame


def sample_negatives(frame: pd.DataFrame, fraction: float, until: pd.Timestamp, rng) -> pd.DataFrame:
    """Keep every positive and a share of the negatives up to a date, with a weight that scales kept negatives back up."""
    negative = (frame["target"] == 0) & (frame["delivery_date"] <= until)
    frame = frame[~negative | (rng.random(len(frame)) < fraction)].copy()
    sampled = (frame["target"] == 0) & (frame["delivery_date"] <= until)
    frame["weight"] = np.where(sampled, 1 / fraction, 1.0)
    return frame


def build_dataset(
    config: Config,
    market: pd.DataFrame,
    constraints: pd.DataFrame,
    units: pd.DataFrame,
    weather: pd.DataFrame,
    points: pd.DataFrame,
    redispatch: pd.DataFrame,
    label_days: set,
    label_end: pd.Timestamp | None,
    dates: list[pd.Timestamp] | None = None,
    sample: tuple[float, int, pd.Timestamp] | None = None,
    hourly: bool = False,
) -> pd.DataFrame:
    """Join national inputs, unit descriptors and weather to every unit with the target, one month at a time; hourly marks rows that each stand for a whole hour."""
    national = market.merge(constraints, on=MARKET_KEY, how="left")
    national["delivery_date"] = pd.to_datetime(national["delivery_date"]).dt.normalize()
    national = national.rename(columns={**SOURCE_NAMES, **{name: spain_redispatch_name(name) for name in config.constraint_indicators}})
    for k in (1, 2, 3):
        national[f"intraday{k}_minus_day_ahead_price"] = national[f"intraday{k}_price"] - national["day_ahead_price"]
    national["demand_minus_wind_solar_mw"] = (
        national["demand_forecast_mw"] - national["wind_generation_forecast_mw"] - national["solar_generation_forecast_mw"]
    )
    numeric = national.columns.difference(MARKET_KEY)
    national[numeric] = national[numeric].astype("float32")
    units = units[["unit"] + UNIT_COLUMNS].copy()
    keys = national[MARKET_KEY].merge(units[["unit"]], how="cross")
    labels = redispatch_labels(config, keys, redispatch[redispatch["unit"].isin(set(units["unit"]))], label_days)
    del keys
    if dates is not None:
        national = national[national["delivery_date"].isin(pd.to_datetime(dates))]

    rng = np.random.default_rng(sample[1]) if sample else None
    parts = []
    for _, month in national.groupby(national["delivery_date"].dt.to_period("M")):
        frame = month.merge(units, how="cross")
        frame["quarter_hour"] = frame["period"].astype("int8")
        frame["day_of_week"] = frame["delivery_date"].dt.weekday.astype("int8")
        frame["month"] = frame["delivery_date"].dt.month.astype("int8")
        frame["is_weekend"] = (frame["day_of_week"] >= 5).astype("int8")
        frame["is_hourly"] = np.int8(hourly)
        frame = add_weather(frame, weather[weather["delivery_date"].isin(month["delivery_date"].unique())], points)
        frame = frame.merge(labels, on=KEY, how="left")
        target = frame["redispatched"] if label_end is not None else pd.Series(np.nan, index=frame.index)
        frame["target"] = target.where(frame["delivery_date"] <= label_end) if label_end is not None else target
        frame["target_mwh"] = frame["redispatch_mwh"].where(frame["target"].notna())
        frame = frame.drop(columns=["redispatched", "redispatch_mwh"])
        if sample:
            fraction, _, until = sample
            frame = sample_negatives(frame[frame["target"].notna()], fraction, until, rng)
        wide = frame.select_dtypes("float64").columns.drop("target", errors="ignore")
        frame[wide] = frame[wide].astype("float32")
        parts.append(frame)
    if not parts:
        return pd.DataFrame(columns=KEY + ["target"])
    return pd.concat(parts, ignore_index=True).sort_values(KEY).reset_index(drop=True)
