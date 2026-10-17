from __future__ import annotations

import numpy as np
import pandas as pd

from .areas import CAPACITY_COLUMNS
from .config import Config
from .rolling import lagged_rolling
from .timeseries import MARKET_KEY
from .weather import VARIABLES, solar_output, wind_output

KEY = MARKET_KEY + ["area"]
CATEGORICAL = ["area_code"]
WEATHER_COLUMNS = list(VARIABLES) + ["expected_wind_mw", "expected_solar_mw", "expected_renewable_mw", "expected_renewable_share"]


def feature_columns(config: Config) -> list[str]:
    """List the model inputs, which depend on the configured indicators and IDA3 usage."""
    prices = ["da_price", "ida1_price", "ida2_price", "ida1_spread", "ida2_spread"]
    if config.features.use_ida3:
        prices += ["ida3_price", "ida3_spread"]
    return (
        ["area_code", "period", "hour", "weekday", "month", "is_weekend"]
        + prices
        + ["load_forecast_mw", "wind_forecast_mw", "solar_forecast_mw", "residual_load_mw"]
        + list(config.constraint_indicators)
        + ["cell_lat", "cell_lon", "mw_total"] + CAPACITY_COLUMNS
        + WEATHER_COLUMNS
        + ["area_congested_hist", "area_congested_hist_long", "area_period_congested_hist"]
    )


def add_weather(frame: pd.DataFrame, weather: pd.DataFrame, points: pd.DataFrame) -> pd.DataFrame:
    """Attach each cell's weather forecast and the wind and solar output it implies for the cell's capacity."""
    weather = weather.merge(points, on="point", how="inner").drop(columns="point")
    frame = frame.merge(weather, on=["delivery_date", "period", "area"], how="left")
    frame["expected_wind_mw"] = frame["mw_wind"] * wind_output(frame["wind_speed_100m"])
    frame["expected_solar_mw"] = frame["mw_solar"] * solar_output(frame["shortwave_radiation"])
    frame["expected_renewable_mw"] = frame["expected_wind_mw"] + frame["expected_solar_mw"]
    national = frame.groupby(MARKET_KEY)["expected_renewable_mw"].transform("sum")
    frame["expected_renewable_share"] = frame["expected_renewable_mw"] / national.where(national > 0)
    return frame


def label_rows(keys: pd.DataFrame, attributed: pd.DataFrame, label_days: set, min_mwh: float) -> pd.Series:
    """Flag keys whose attributed redispatch reaches the threshold; days without a file stay unknown."""
    merged = keys[KEY].merge(attributed, on=KEY, how="left")
    congested = (merged["mwh"].fillna(0.0) >= min_mwh).astype(float)
    known = merged["delivery_date"].isin(label_days)
    return congested.where(known.to_numpy(), np.nan)


def congestion_history(config: Config, keys: pd.DataFrame, attributed: pd.DataFrame, label_days: set) -> pd.DataFrame:
    """Label every key with its flag and MWh and compute each cell's lagged congestion history on a compact table."""
    frame = keys.sort_values(KEY).reset_index(drop=True)
    frame["congested"] = label_rows(frame, attributed, label_days, config.labels.min_mwh).to_numpy().astype("float32")
    mwh = frame[KEY].merge(attributed[KEY + ["mwh"]], on=KEY, how="left")["mwh"].fillna(0.0).clip(lower=0.0)
    frame["redispatch_mwh"] = mwh.where(frame["congested"].notna().to_numpy()).to_numpy().astype("float32")
    days = config.features.history_days
    lag = config.features.event_lag_days
    frame["area_period_congested_hist"] = lagged_rolling(frame, "congested", ["area", "period"], days, lag, "mean").astype("float32")
    daily = frame.groupby(["delivery_date", "area"], as_index=False)["congested"].mean()
    daily["area_congested_hist"] = lagged_rolling(daily, "congested", ["area"], days, lag, "mean").astype("float32")
    daily["area_congested_hist_long"] = lagged_rolling(daily, "congested", ["area"], 4 * days, lag, "mean").astype("float32")
    return frame.merge(daily.drop(columns="congested"), on=["delivery_date", "area"], how="left")


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
    capacity: pd.DataFrame,
    weather: pd.DataFrame,
    points: pd.DataFrame,
    attributed: pd.DataFrame,
    label_days: set,
    label_end: pd.Timestamp | None,
    area_codes: dict[str, int],
    dates: list[pd.Timestamp] | None = None,
    sample: tuple[float, int, pd.Timestamp] | None = None,
) -> pd.DataFrame:
    """Join national inputs to every area with lagged congestion history and the target, one month at a time."""
    national = market.merge(constraints, on=MARKET_KEY, how="left")
    national["delivery_date"] = pd.to_datetime(national["delivery_date"]).dt.normalize()
    for k in (1, 2, 3):
        national[f"ida{k}_spread"] = national[f"ida{k}_price"] - national["da_price"]
    national["residual_load_mw"] = (
        national["load_forecast_mw"] - national["wind_forecast_mw"] - national["solar_forecast_mw"]
    )
    numeric = national.columns.difference(MARKET_KEY)
    national[numeric] = national[numeric].astype("float32")
    capacity = capacity[capacity["area"].isin(area_codes)].copy()
    capacity["area_code"] = capacity["area"].map(area_codes)
    wide = capacity.select_dtypes("float64").columns
    capacity[wide] = capacity[wide].astype("float32")

    keys = national[MARKET_KEY].merge(capacity[["area"]], how="cross")
    history = congestion_history(config, keys, attributed, label_days)
    del keys
    if dates is not None:
        national = national[national["delivery_date"].isin(pd.to_datetime(dates))]

    rng = np.random.default_rng(sample[1]) if sample else None
    parts = []
    for _, month in national.groupby(national["delivery_date"].dt.to_period("M")):
        frame = month.merge(capacity, how="cross")
        frame["hour"] = (frame["period"] // 4).astype("int8")
        frame["weekday"] = frame["delivery_date"].dt.weekday.astype("int8")
        frame["month"] = frame["delivery_date"].dt.month.astype("int8")
        frame["is_weekend"] = (frame["weekday"] >= 5).astype("int8")
        frame = add_weather(frame, weather[weather["delivery_date"].isin(month["delivery_date"].unique())], points)
        frame = frame.merge(history, on=KEY, how="left")
        target = frame["congested"] if label_end is not None else pd.Series(np.nan, index=frame.index)
        frame["target"] = target.where(frame["delivery_date"] <= label_end) if label_end is not None else target
        frame["target_mwh"] = frame["redispatch_mwh"].where(frame["target"].notna())
        frame = frame.drop(columns=["congested", "redispatch_mwh"])
        if sample:
            fraction, _, until = sample
            frame = sample_negatives(frame[frame["target"].notna()], fraction, until, rng)
        wide = frame.select_dtypes("float64").columns.drop("target", errors="ignore")
        frame[wide] = frame[wide].astype("float32")
        parts.append(frame)
    if not parts:
        return pd.DataFrame(columns=KEY + ["target"])
    return pd.concat(parts, ignore_index=True).sort_values(KEY).reset_index(drop=True)
