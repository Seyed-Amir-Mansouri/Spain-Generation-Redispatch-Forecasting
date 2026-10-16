from __future__ import annotations

import tomllib
from dataclasses import dataclass
from pathlib import Path

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
class Areas:
    cell_degrees: float
    min_located_mw: float
    estimate_unplaced: bool = False
    estimate_top_cells: int = 3


@dataclass(frozen=True)
class Weather:
    grid_degrees: float
    points_per_request: int
    request_interval_seconds: float


@dataclass(frozen=True)
class Labels:
    direction: str
    restriction_types: tuple[str, ...]
    min_mwh: float
    settle_days: int


@dataclass(frozen=True)
class Features:
    history_days: int
    event_lag_days: int
    use_ida3: bool


@dataclass(frozen=True)
class Validation:
    window_days: int
    min_skill: float


@dataclass(frozen=True)
class Config:
    root: Path
    paths: Paths
    request_interval_seconds: float
    areas: Areas
    weather: Weather
    labels: Labels
    features: Features
    constraint_indicators: dict[str, int]
    model: Settings
    validation: Validation

    def resolve(self, path: Path) -> Path:
        """Return an absolute path relative to the config file's folder."""
        return path if path.is_absolute() else self.root / path


def load_config(path: str | Path = DEFAULT_CONFIG) -> Config:
    """Parse a TOML config file into a Config object."""
    path = Path(path).resolve()
    with path.open("rb") as handle:
        raw = tomllib.load(handle)
    labels = raw["labels"]
    return Config(
        root=path.parent,
        paths=Paths(**{key: Path(value) for key, value in raw["paths"].items()}),
        request_interval_seconds=float(raw["esios"]["request_interval_seconds"]),
        areas=Areas(**raw["areas"]),
        weather=Weather(**raw["weather"]),
        labels=Labels(
            direction=labels["direction"],
            restriction_types=tuple(labels["restriction_types"]),
            min_mwh=float(labels["min_mwh"]),
            settle_days=int(labels["settle_days"]),
        ),
        features=Features(**raw["features"]),
        constraint_indicators={name: int(value) for name, value in raw["constraint_indicators"].items()},
        model=Settings(**raw["model"]),
        validation=Validation(**raw["validation"]),
    )
