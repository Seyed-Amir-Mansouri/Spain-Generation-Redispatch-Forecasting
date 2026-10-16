from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone

import pandas as pd

from . import areas, estimator, i90, ledger, locations, weather
from .config import Config
from .features import CATEGORICAL, KEY, build_dataset, feature_columns

MAX_CATEGORIES = 254


@dataclass(frozen=True)
class Inputs:
    market: pd.DataFrame
    constraints: pd.DataFrame
    capacity: pd.DataFrame
    weather: pd.DataFrame
    points: pd.DataFrame
    attributed: pd.DataFrame
    label_days: set
    area_codes: dict[str, int]
    coverage: float


def _read_hourly(path) -> pd.DataFrame:
    """Read an hourly national CSV written by the fetch commands."""
    if not path.exists():
        raise FileNotFoundError(f"Input file not found: {path}; run the fetch commands first")
    return pd.read_csv(path, parse_dates=["delivery_date"])


def load_inputs(config: Config) -> Inputs:
    """Read market, constraint, unit-location and redispatch files and attribute redispatch to areas."""
    programming, _ = locations.load_registry(config.resolve(config.paths.registry_dir))
    units = pd.read_csv(config.resolve(config.paths.unit_locations))
    size = config.areas.cell_degrees
    shares = areas.unit_shares(units, programming, size)
    capacity = areas.capacity_by_area(units, size, config.areas.min_located_mw)

    redispatch, days = i90.load(
        config.resolve(config.paths.redispatch_dir), config.labels.direction, config.labels.restriction_types
    )
    attributed = areas.attribute(redispatch, shares) if len(redispatch) else pd.DataFrame(columns=KEY + ["mwh"])
    points, _ = weather.points_for(capacity, config.weather.grid_degrees)
    return Inputs(
        market=_read_hourly(config.resolve(config.paths.market)),
        constraints=_read_hourly(config.resolve(config.paths.constraints)),
        capacity=capacity,
        weather=_read_hourly(config.resolve(config.paths.weather)),
        points=points,
        attributed=attributed,
        label_days=set(days),
        area_codes={area: code for code, area in enumerate(sorted(capacity["area"]))},
        coverage=areas.coverage(redispatch, shares) if len(redispatch) else float("nan"),
    )


def label_cutoff(config: Config, today: pd.Timestamp | None = None) -> pd.Timestamp:
    """Return the last delivery date whose per-unit redispatch has been published."""
    if today is None:
        today = pd.Timestamp(datetime.now(timezone.utc).date())
    return today.normalize() - pd.Timedelta(days=config.labels.settle_days)


def build(config: Config, inputs: Inputs, label_end, area_codes: dict[str, int] | None = None) -> pd.DataFrame:
    """Build the feature table for every date and hour present in the market inputs."""
    return build_dataset(
        config,
        inputs.market,
        inputs.constraints,
        inputs.capacity,
        inputs.weather,
        inputs.points,
        inputs.attributed,
        inputs.label_days,
        label_end,
        area_codes or inputs.area_codes,
    )


def train(config: Config, inputs: Inputs, dataset: pd.DataFrame) -> tuple[dict, dict]:
    """Train the Spanish classifier, treating the grid cell as a categorical input."""
    categorical = CATEGORICAL if len(inputs.area_codes) <= MAX_CATEGORIES else []
    bundle, report = estimator.train(
        dataset,
        feature_columns(config),
        config.model,
        categorical=categorical,
        extra={"area_codes": inputs.area_codes, "cell_degrees": config.areas.cell_degrees},
    )
    report["areas"] = len(inputs.area_codes)
    report["redispatch_coverage"] = round(inputs.coverage, 3)
    labelled = dataset[dataset["target"].notna()]
    report["labelled_days"] = int(labelled["delivery_date"].nunique())
    report["positive_rate"] = round(float(labelled["target"].mean()), 4) if len(labelled) else None
    return bundle, report


def predict_day(
    bundle: dict,
    dataset: pd.DataFrame,
    day: pd.Timestamp,
    issued_at: datetime | None = None,
) -> pd.DataFrame:
    """Score one delivery day and return rows ready for the prediction ledger."""
    rows = dataset[dataset["delivery_date"] == day].copy()
    if rows.empty:
        raise ValueError(f"No inputs found for delivery date {day:%Y-%m-%d}")
    issued_at = issued_at or datetime.now(timezone.utc)
    rows["risk_probability"] = estimator.predict(bundle, rows)
    rows["risk_flag"] = (rows["risk_probability"] >= bundle["threshold"]).astype(int)
    rows["issued_at"] = issued_at.isoformat()
    rows["model_version"] = bundle["model_version"]
    return rows[ledger.PREDICTION_COLUMNS].reset_index(drop=True)
