from __future__ import annotations

import numpy as np
import pandas as pd

from .config import Config
from .generation import COLUMNS as GENERATION_COLUMNS
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
ROUTINE_COLUMNS = [f"unit_{name}_rate_{span}" for name in ("down", "up") for span in ("month_hour", "hour")]


def spain_redispatch_name(indicator: str) -> str:
    """Name an REE Phase I indicator as Spain's total redispatch, e.g. spain_redispatch_down_overload_normal_mwh."""
    name = indicator.replace("phase1_", "").replace("overload_base", "overload_normal")
    return f"spain_redispatch_{name}_mwh"


def spain_schedule_name(indicator: str) -> str:
    """Name an REE day-ahead schedule indicator as Spain's scheduled energy, e.g. spain_schedule_combined_cycle_mwh."""
    return f"spain_schedule_{indicator.removeprefix('pbf_')}_mwh"


def feature_columns(config: Config) -> list[str]:
    """List the model inputs, which depend on the configured indicators and IDA3 usage."""
    auctions = (1, 2, 3) if config.features.use_ida3 else (1, 2)
    return (
        UNIT_COLUMNS
        + ["quarter_hour", "day_of_week", "month", "is_weekend", "day_ahead_price"]
        + [f"intraday{k}_price" for k in auctions]
        + [f"intraday{k}_minus_day_ahead_price" for k in auctions]
        + ["demand_forecast_mw", "wind_generation_forecast_mw", "solar_generation_forecast_mw", "demand_minus_wind_solar_mw"]
        + [spain_redispatch_name(name) for name in config.constraint_indicators]
        + [spain_schedule_name(name) for name in config.schedule_indicators]
        + WEATHER_COLUMNS
        + GENERATION_COLUMNS
        + ROUTINE_COLUMNS
    )


def add_weather(frame: pd.DataFrame, weather: pd.DataFrame, points: pd.DataFrame) -> pd.DataFrame:
    """Attach the weather forecast of each unit's area."""
    weather = weather.merge(points, on="point", how="inner").drop(columns="point").rename(columns=SOURCE_NAMES)
    return frame.merge(weather, on=KEY, how="left")


def add_generation(frame: pd.DataFrame, unit_quarters: pd.DataFrame | None, unit_days: pd.DataFrame | None) -> pd.DataFrame:
    """Attach each unit's recent ENTSO-E output; units and days without it stay empty."""
    days = frame["delivery_date"].unique()
    if unit_quarters is not None:
        frame = frame.merge(unit_quarters[unit_quarters["delivery_date"].isin(days)], on=KEY, how="left")
    if unit_days is not None:
        frame = frame.merge(unit_days[unit_days["delivery_date"].isin(days)], on=["delivery_date", "unit"], how="left")
    missing = [column for column in GENERATION_COLUMNS if column not in frame]
    frame[missing] = np.nan
    return frame


def routine_rates(moved: dict[str, pd.DataFrame], units: list[str], start: pd.Timestamp, end: pd.Timestamp) -> pd.DataFrame:
    """Share of days from start to end each unit was moved in each direction at each hour: by month, weekday or weekend
    and hour (unit_down_rate_month_hour), and by hour over the whole span (unit_down_rate_hour). moved holds the
    delivery_date, hour and unit of every moved unit-hour per direction name."""
    days = pd.DataFrame({"delivery_date": pd.date_range(start, end)})
    days["month"], days["is_weekend"] = days["delivery_date"].dt.month, (days["delivery_date"].dt.weekday >= 5).astype(int)
    grid = days.merge(pd.DataFrame({"hour": range(24)}), how="cross").merge(pd.DataFrame({"unit": units}), how="cross")
    keys = ["unit", "month", "is_weekend", "hour"]
    rates = grid[keys].drop_duplicates()
    for name, rows in moved.items():
        flags = grid.merge(rows[["delivery_date", "hour", "unit"]].drop_duplicates().assign(moved=1.0), on=["delivery_date", "hour", "unit"], how="left")
        flags["moved"] = flags["moved"].fillna(0.0)
        by_month = flags.groupby(keys, as_index=False)["moved"].mean().rename(columns={"moved": f"unit_{name}_rate_month_hour"})
        by_hour = flags.groupby(["unit", "hour"], as_index=False)["moved"].mean().rename(columns={"moved": f"unit_{name}_rate_hour"})
        rates = rates.merge(by_month, on=keys).merge(by_hour, on=["unit", "hour"])
    return rates


def add_routine(frame: pd.DataFrame, routine: pd.DataFrame | None) -> pd.DataFrame:
    """Attach each unit's routine for the row's month, weekday or weekend and hour; months outside the routine's span keep only the hourly rate."""
    if routine is not None:
        frame["hour"] = frame["period"] // 4
        by_month = routine.drop(columns=[c for c in routine if c.endswith("_rate_hour")])
        by_hour = routine[["unit", "hour"] + [c for c in routine if c.endswith("_rate_hour")]].drop_duplicates(["unit", "hour"])
        frame = frame.merge(by_month, on=["unit", "month", "is_weekend", "hour"], how="left").merge(by_hour, on=["unit", "hour"], how="left")
        frame = frame.drop(columns="hour")
    missing = [column for column in ROUTINE_COLUMNS if column not in frame]
    frame[missing] = np.nan
    return frame


def label_rows(keys: pd.DataFrame, redispatch: pd.DataFrame, label_days: set, min_mwh: float) -> pd.Series:
    """Flag keys whose redispatch reaches the threshold; days without a file stay unknown."""
    merged = keys[KEY].merge(redispatch, on=KEY, how="left")
    redispatched = (merged["mwh"].fillna(0.0) >= min_mwh).astype(float)
    known = merged["delivery_date"].isin(label_days)
    return redispatched.where(known.to_numpy(), np.nan)


def redispatch_labels(config: Config, keys: pd.DataFrame, redispatch: pd.DataFrame, label_days: set) -> pd.DataFrame:
    """Give every key its redispatched flag, unknown on days without published redispatch."""
    frame = keys.sort_values(KEY).reset_index(drop=True)
    frame["redispatched"] = label_rows(frame, redispatch, label_days, config.labels.min_mwh).to_numpy().astype("float32")
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
    unit_quarters: pd.DataFrame | None = None,
    unit_days: pd.DataFrame | None = None,
    schedule: pd.DataFrame | None = None,
    routine: pd.DataFrame | None = None,
) -> pd.DataFrame:
    """Join national inputs, unit descriptors, weather, each unit's recent output and routine to every unit with the target, one month at a time."""
    national = market.merge(constraints, on=MARKET_KEY, how="left")
    if schedule is not None:
        national = national.merge(schedule[MARKET_KEY + [name for name in config.schedule_indicators if name in schedule]], on=MARKET_KEY, how="left")
    for name in config.schedule_indicators:
        if name not in national:
            national[name] = np.nan
    national["delivery_date"] = pd.to_datetime(national["delivery_date"]).dt.normalize()
    national = national.rename(columns={
        **SOURCE_NAMES,
        **{name: spain_redispatch_name(name) for name in config.constraint_indicators},
        **{name: spain_schedule_name(name) for name in config.schedule_indicators},
    })
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
        frame = add_weather(frame, weather[weather["delivery_date"].isin(month["delivery_date"].unique())], points)
        frame = add_generation(frame, unit_quarters, unit_days)
        frame = add_routine(frame, routine)
        frame = frame.merge(labels, on=KEY, how="left")
        target = frame["redispatched"] if label_end is not None else pd.Series(np.nan, index=frame.index)
        frame["target"] = target.where(frame["delivery_date"] <= label_end) if label_end is not None else target
        frame = frame.drop(columns="redispatched")
        if sample:
            fraction, _, until = sample
            frame = sample_negatives(frame[frame["target"].notna()], fraction, until, rng)
        wide = frame.select_dtypes("float64").columns.drop("target", errors="ignore")
        frame[wide] = frame[wide].astype("float32")
        parts.append(frame)
    if not parts:
        return pd.DataFrame(columns=KEY + ["target"])
    return pd.concat(parts, ignore_index=True).sort_values(KEY).reset_index(drop=True)
