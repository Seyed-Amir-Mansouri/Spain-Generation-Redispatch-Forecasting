from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone

import numpy as np
import pandas as pd

from . import estimator, i90, ledger, locations, metrics, weather
from .config import Config
from .features import CATEGORICAL, KEY, build_dataset, feature_columns

DIRECTION_NAMES = {"Bajar": "down", "Subir": "up"}
WEATHER_TECHNOLOGIES = ("wind", "solar_pv", "solar_thermal", "hybrid")


@dataclass(frozen=True)
class Inputs:
    market: pd.DataFrame
    constraints: pd.DataFrame
    units: pd.DataFrame
    weather: pd.DataFrame
    points: pd.DataFrame
    redispatch: pd.DataFrame
    label_days: set
    coverage: float


def _read_table(path, start: pd.Timestamp, end: pd.Timestamp | None = None) -> pd.DataFrame:
    """Read a CSV written by the fetch commands, keeping delivery dates from start up to end."""
    if not path.exists():
        raise FileNotFoundError(f"Input file not found: {path}; run the fetch commands first")
    frame = pd.read_csv(path, parse_dates=["delivery_date"])
    keep = (frame["delivery_date"] >= start) & ((frame["delivery_date"] < end) if end is not None else True)
    return frame[keep].reset_index(drop=True)


def _read_input(config: Config, name: str) -> pd.DataFrame:
    """Read one quarter-hour input from the first 15-minute day."""
    return _read_table(config.resolve(getattr(config.paths, name)), config.start)


def _read_redispatch(config: Config) -> tuple[pd.DataFrame, list[pd.Timestamp]]:
    """Load the config direction's redispatch rows and the days covered from start."""
    redispatch, days = i90.load(config.resolve(config.paths.redispatch_dir), config.labels.direction, config.labels.restriction_types)
    days = [day for day in days if day >= config.start]
    return redispatch[redispatch["delivery_date"].isin(days)], days


def unit_places(config: Config) -> pd.DataFrame:
    """Place each programming unit at the capacity-weighted centre of its located physical units."""
    units = pd.read_csv(config.resolve(config.paths.unit_locations))
    units = units[units["lat"].notna()].assign(mw=units["max_mw"].fillna(0).clip(lower=0) + 1e-6)
    units["wlat"], units["wlon"] = units["lat"] * units["mw"], units["lon"] * units["mw"]
    sums = units.groupby("programming_unit")[["wlat", "wlon", "mw"]].sum()
    return pd.DataFrame({"unit": sums.index, "lat": sums["wlat"] / sums["mw"], "lon": sums["wlon"] / sums["mw"]}).reset_index(drop=True)


def weather_spots(config: Config, units: pd.DataFrame) -> pd.DataFrame:
    """Return the weather spots of the placed wind and solar units among those predicted."""
    ids = [locations.TECHNOLOGIES.index(name) for name in WEATHER_TECHNOLOGIES]
    places = unit_places(config)
    green = places[places["unit"].isin(set(units.loc[units["technology_id"].isin(ids), "unit"]))]
    return weather.spots_for(green, config.weather.spot_degrees)


def frequent_units(config: Config, redispatch: pd.DataFrame, days: list[pd.Timestamp]) -> set[str]:
    """Return the units redispatched in at least min_events quarter-hours before the held-out days."""
    cutoff = max(days) - pd.Timedelta(days=config.model.test_days)
    early = redispatch[(redispatch["delivery_date"] <= cutoff) & (redispatch["mwh"] >= config.labels.min_mwh)]
    counts = early.groupby("unit").size()
    return set(counts[counts >= config.units.min_events].index)


def located_units(config: Config, names: set[str]) -> set[str]:
    """Keep the units whose physical units were all located with the configured confidence, or all units when it is unset."""
    wanted = config.units.location_confidence
    if not wanted:
        return names
    physical = pd.read_csv(config.resolve(config.paths.unit_locations))
    sure = physical.groupby("programming_unit")["confidence"].agg(lambda values: bool((values == wanted).all()))
    return names & set(sure[sure].index)


def unit_technology(config: Config, units: pd.Series) -> pd.Series:
    """Return each programming unit's grouped production type from REE's registry."""
    programming, _ = locations.load_registry(config.resolve(config.paths.registry_dir))
    registered = programming.drop_duplicates("code").set_index("code")["technology"]
    return units.map(registered).map(locations.technology_group)


def describe_units(config: Config, names) -> pd.DataFrame:
    """Give each unit a stable id and its technology id."""
    units = pd.DataFrame({"unit": sorted(names)})
    units["unit_id"] = np.arange(len(units), dtype="int32")
    units["technology_id"] = unit_technology(config, units["unit"]).map(locations.TECHNOLOGIES.index).astype("int16")
    return units


def select_units(config: Config) -> pd.DataFrame:
    """Pick the located units frequently redispatched up or down; both directions predict for the same units."""
    names = set()
    for direction in config.labels.directions:
        redispatch, days = load_redispatch(config.for_direction(direction))
        names |= frequent_units(config, redispatch, days)
    return describe_units(config, located_units(config, names))


def load_redispatch(config: Config) -> tuple[pd.DataFrame, list[pd.Timestamp]]:
    """Read the config direction's per-unit redispatch per quarter-hour, with unit aliases applied, and the days covered."""
    redispatch, days = _read_redispatch(config)
    redispatch = redispatch.copy()
    redispatch["unit"] = redispatch["unit"].replace(locations.load_aliases(config.resolve(config.paths.unit_aliases)))
    return redispatch.groupby(KEY, as_index=False)["mwh"].sum(), days


def load_inputs(config: Config, units: pd.DataFrame | None = None) -> Inputs:
    """Read market, constraint, weather and per-unit redispatch files, and pick the units to predict."""
    redispatch, days = load_redispatch(config)
    if units is None:
        units = select_units(config)
    total = redispatch["mwh"].sum()
    covered = redispatch.loc[redispatch["unit"].isin(set(units["unit"])), "mwh"].sum() / total if total else float("nan")
    places = unit_places(config)
    places = places[places["unit"].isin(set(units["unit"]))]
    points, area = weather.area_weather(places, weather_spots(config, units), _read_input(config, "weather"), config.weather.area_degrees)
    return Inputs(
        market=_read_input(config, "market"),
        constraints=_read_input(config, "constraints"),
        units=units,
        weather=area,
        points=points,
        redispatch=redispatch,
        label_days=set(days),
        coverage=covered,
    )


def label_cutoff(config: Config, today: pd.Timestamp | None = None) -> pd.Timestamp:
    """Return the last delivery date whose per-unit redispatch has been published."""
    if today is None:
        today = pd.Timestamp(datetime.now(timezone.utc).date())
    return today.normalize() - pd.Timedelta(days=config.labels.settle_days)


def build(config: Config, inputs: Inputs, label_end, dates=None) -> pd.DataFrame:
    """Build the feature table for the given dates, or every date in the market inputs, at quarter-hours."""
    return build_dataset(
        config, inputs.market, inputs.constraints, inputs.units, inputs.weather, inputs.points,
        inputs.redispatch, inputs.label_days, label_end, dates,
    )


def build_training(config: Config, inputs: Inputs, label_end: pd.Timestamp) -> pd.DataFrame:
    """Build only the labelled rows, sampling negatives before the holdout days as each month is built."""
    last = max(day for day in inputs.label_days if day <= label_end)
    cutoff = last - pd.Timedelta(days=config.model.test_days)
    sample = (config.model.negative_fraction, config.model.random_state, cutoff)
    return build_dataset(
        config, inputs.market, inputs.constraints, inputs.units, inputs.weather, inputs.points,
        inputs.redispatch, inputs.label_days, label_end, sample=sample,
    )


def train(config: Config, inputs: Inputs, dataset: pd.DataFrame) -> tuple[dict, dict, pd.DataFrame | None]:
    """Train one direction's yes/no model per unit-quarter, and return its scored holdout rows."""
    bundle, report, holdout = estimator.train(
        dataset,
        feature_columns(config),
        config.model,
        categorical=CATEGORICAL,
        extra={"units": inputs.units, "direction": config.labels.direction},
    )
    report["direction"] = config.labels.direction
    report["units"] = len(inputs.units)
    report["redispatch_coverage"] = round(inputs.coverage, 3)
    labelled = dataset[dataset["target"].notna()]
    report["labelled_days"] = int(labelled["delivery_date"].nunique())
    weights = labelled["weight"] if "weight" in labelled else None
    report["positive_rate"] = round(float(np.average(labelled["target"], weights=weights)), 4) if len(labelled) else None
    return bundle, report, holdout


def holdout_decisions(holdouts: dict[str, pd.DataFrame], threshold: float) -> pd.DataFrame:
    """Join each direction's scored holdout rows and call every unit-quarter up, down or nothing."""
    down = holdouts["Bajar"].rename(columns={"target": "down_target", "probability": "down_probability"})
    up = holdouts["Subir"].rename(columns={"target": "up_target", "probability": "up_probability"})
    frame = down.merge(up, on=KEY, how="inner")
    frame["actual"] = np.select([frame["up_target"] == 1, frame["down_target"] == 1], ["up", "down"], "nothing")
    frame["decision"] = metrics.decide(frame["down_probability"], frame["up_probability"], threshold)
    return frame


def predict_day(
    bundles: dict[str, dict],
    dataset: pd.DataFrame,
    day: pd.Timestamp,
    threshold: float,
    issued_at: datetime | None = None,
) -> pd.DataFrame:
    """Score one delivery day with the downward and upward models and call each unit-quarter up, down or nothing."""
    rows = dataset[dataset["delivery_date"] == day].copy()
    if rows.empty:
        raise ValueError(f"No inputs found for delivery date {day:%Y-%m-%d}")
    issued_at = issued_at or datetime.now(timezone.utc)
    for direction, name in DIRECTION_NAMES.items():
        rows[f"{name}_probability"] = estimator.predict(bundles[direction], rows)
    rows["decision"] = metrics.decide(rows["down_probability"], rows["up_probability"], threshold)
    rows["issued_at"] = issued_at.isoformat()
    rows["model_version"] = ";".join(f"{name}:{bundles[direction]['model_version']}" for direction, name in DIRECTION_NAMES.items())
    return rows[ledger.PREDICTION_COLUMNS].reset_index(drop=True)
