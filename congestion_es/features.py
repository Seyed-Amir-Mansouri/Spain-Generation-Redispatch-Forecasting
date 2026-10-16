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
        ["area_code", "hour", "weekday", "month", "is_weekend"]
        + prices
        + ["load_forecast_mw", "wind_forecast_mw", "solar_forecast_mw", "residual_load_mw"]
        + list(config.constraint_indicators)
        + ["cell_lat", "cell_lon", "mw_total"] + CAPACITY_COLUMNS
        + WEATHER_COLUMNS
        + ["area_congested_hist", "area_congested_hist_long", "area_hour_congested_hist"]
    )


def add_weather(frame: pd.DataFrame, weather: pd.DataFrame, points: pd.DataFrame) -> pd.DataFrame:
    """Attach each cell's weather forecast and the wind and solar output it implies for the cell's capacity."""
    frame = frame.merge(points, on="area", how="left")
    frame = frame.merge(weather, on=["delivery_date", "hour", "point"], how="left").drop(columns="point")
    frame["expected_wind_mw"] = frame["mw_wind"] * wind_output(frame["wind_speed_100m"])
    frame["expected_solar_mw"] = frame["mw_solar"] * solar_output(frame["shortwave_radiation"])
    frame["expected_renewable_mw"] = frame["expected_wind_mw"] + frame["expected_solar_mw"]
    national = frame.groupby(["delivery_date", "hour"])["expected_renewable_mw"].transform("sum")
    frame["expected_renewable_share"] = frame["expected_renewable_mw"] / national.where(national > 0)
    return frame


def label_rows(keys: pd.DataFrame, attributed: pd.DataFrame, label_days: set, min_mwh: float) -> pd.Series:
    """Flag keys whose attributed redispatch reaches the threshold; days without a file stay unknown."""
    merged = keys[KEY].merge(attributed, on=KEY, how="left")
    congested = (merged["mwh"].fillna(0.0) >= min_mwh).astype(float)
    known = merged["delivery_date"].isin(label_days)
    return congested.where(known.to_numpy(), np.nan)


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
) -> pd.DataFrame:
    """Join national inputs to every area and build lagged congestion history and the target."""
    national = market.merge(constraints, on=MARKET_KEY, how="left")
    for k in (1, 2, 3):
        national[f"ida{k}_spread"] = national[f"ida{k}_price"] - national["da_price"]
    national["residual_load_mw"] = (
        national["load_forecast_mw"] - national["wind_forecast_mw"] - national["solar_forecast_mw"]
    )

    frame = national.merge(capacity, how="cross")
    frame["area_code"] = frame["area"].map(area_codes)
    frame = frame[frame["area_code"].notna()]
    frame["delivery_date"] = pd.to_datetime(frame["delivery_date"]).dt.normalize()
    frame["weekday"] = frame["delivery_date"].dt.weekday
    frame["month"] = frame["delivery_date"].dt.month
    frame["is_weekend"] = (frame["weekday"] >= 5).astype(int)
    frame = add_weather(frame, weather, points)
    frame = frame.sort_values(KEY).reset_index(drop=True)

    frame["congested"] = label_rows(frame, attributed, label_days, config.labels.min_mwh).to_numpy()
    days = config.features.history_days
    lag = config.features.event_lag_days
    frame["area_hour_congested_hist"] = lagged_rolling(frame, "congested", ["area", "hour"], days, lag, "mean")
    daily = frame.groupby(["delivery_date", "area"], as_index=False)["congested"].mean()
    daily["area_congested_hist"] = lagged_rolling(daily, "congested", ["area"], days, lag, "mean")
    daily["area_congested_hist_long"] = lagged_rolling(daily, "congested", ["area"], 4 * days, lag, "mean")
    frame = frame.merge(daily.drop(columns="congested"), on=["delivery_date", "area"], how="left")

    target = frame["congested"]
    if label_end is None:
        target = pd.Series(np.nan, index=frame.index)
    else:
        target = target.where(frame["delivery_date"] <= label_end)
    frame["target"] = target
    return frame.drop(columns="congested")
