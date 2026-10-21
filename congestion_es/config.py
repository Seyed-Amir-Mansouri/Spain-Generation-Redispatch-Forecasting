from __future__ import annotations

import tomllib
from dataclasses import dataclass, field, replace
from pathlib import Path

import pandas as pd

from .estimator import Settings

DEFAULT_CONFIG = Path(__file__).resolve().parents[1] / "config.toml"


@dataclass(frozen=True)
class Paths:
    registry_dir: Path
    plants_dir: Path
    redispatch_dir: Path
    market: Path
    constraints: Path
    weather: Path
    unit_locations: Path
    manual_locations: Path
    unit_aliases: Path
    model_dir: Path
    predictions: Path
    validation: Path


@dataclass(frozen=True)
class Units:
    min_events: int
    location_confidence: str = ""


@dataclass(frozen=True)
class Weather:
    spot_degrees: float
    area_degrees: float
    points_per_request: int
    request_interval_seconds: float
    calls_per_hour: float


@dataclass(frozen=True)
class Labels:
    directions: tuple[str, ...]
    codes: dict[str, tuple[str, ...]]
    min_mwh: float
    settle_days: int
    direction: str = ""
    restriction_types: tuple[str, ...] = ()


@dataclass(frozen=True)
class Features:
    use_ida3: bool


@dataclass(frozen=True)
class Validation:
    window_days: int
    min_skill: float


@dataclass(frozen=True)
class Config:
    root: Path
    paths: Paths
    start: pd.Timestamp
    request_interval_seconds: float
    units: Units
    weather: Weather
    labels: Labels
    features: Features
    constraint_indicators: dict[str, int]
    model: Settings
    validation: Validation
    model_overrides: dict[str, dict] = field(default_factory=dict)

    def resolve(self, path: Path) -> Path:
        """Return an absolute path relative to the config file's folder."""
        return path if path.is_absolute() else self.root / path

    def for_direction(self, direction: str) -> Config:
        """Return a copy that labels and trains on one redispatch direction, with that direction's model settings."""
        return replace(
            self,
            labels=replace(self.labels, direction=direction, restriction_types=self.labels.codes[direction]),
            model=replace(self.model, **self.model_overrides.get(direction, {})),
        )


def load_config(path: str | Path = DEFAULT_CONFIG) -> Config:
    """Parse a TOML config file into a Config object."""
    path = Path(path).resolve()
    with path.open("rb") as handle:
        raw = tomllib.load(handle)
    labels = raw["labels"]
    model = {key: value for key, value in raw["model"].items() if not isinstance(value, dict)}
    overrides = {key: value for key, value in raw["model"].items() if isinstance(value, dict)}
    return Config(
        root=path.parent,
        paths=Paths(**{key: Path(value) for key, value in raw["paths"].items()}),
        start=pd.Timestamp(raw["data"]["start"]),
        request_interval_seconds=float(raw["esios"]["request_interval_seconds"]),
        units=Units(**raw["units"]),
        weather=Weather(**raw["weather"]),
        labels=Labels(
            directions=tuple(labels["directions"]),
            codes={direction: tuple(codes) for direction, codes in labels["restriction_types"].items()},
            direction=labels["directions"][0],
            restriction_types=tuple(labels["restriction_types"][labels["directions"][0]]),
            min_mwh=float(labels["min_mwh"]),
            settle_days=int(labels["settle_days"]),
        ),
        features=Features(**raw["features"]),
        constraint_indicators={name: int(value) for name, value in raw["constraint_indicators"].items()},
        model=Settings(**model),
        validation=Validation(**raw["validation"]),
        model_overrides=overrides,
    )
