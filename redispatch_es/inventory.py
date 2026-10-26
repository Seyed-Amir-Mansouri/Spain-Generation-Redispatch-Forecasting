"""Work out which inputs a forecast day needs, which of them are already on disk, and remember what was downloaded."""
from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path

import pandas as pd

from . import estimator, features, pipeline
from .config import Config

DATASETS = [
    ("market", "Day-ahead price and Spain's demand, wind and solar forecasts", "ENTSO-E"),
    ("intraday", "Intraday auction prices (IDA1, IDA2, IDA3)", "OMIE"),
    ("constraints", "National Phase I redispatch totals", "REE e-sios"),
    ("schedule", "Day-ahead schedule (PBF) per technology", "REE e-sios"),
    ("weather", "Weather forecast at the wind and solar sites", "Open-Meteo"),
    ("generation", "Actual output of each unit, the two days before", "ENTSO-E"),
]
LOG_NAME = "download_log.json"
FULL_DAY = 92
FINAL_AFTER_DAYS = 3
RETRY_MINUTES = 30
TIMEZONE = "Europe/Madrid"
PUBLISHED = {
    "market": (-1, "18:00", "Day-ahead price about 13:00, wind and solar forecasts by 18:00 the day before"),
    "constraints": (-1, "16:00", "REE solves the Phase I constraints the afternoon before"),
    "schedule": (-1, "14:00", "REE publishes the day-ahead schedule (PBF) early in the afternoon before"),
    "weather": (-2, "00:00", "Forecasts are available any time, days ahead"),
}
INTRADAY = {True: (0, "10:30", "IDA3 clears on the delivery day about 10:30, for hours 13 to 24"),
            False: (-1, "22:30", "IDA2 clears the day before about 22:30")}


def log_path(config: Config) -> Path:
    """Return the download log, kept next to the downloaded market file."""
    return config.resolve(config.paths.market).parent / LOG_NAME


def read_log(config: Config) -> dict:
    path = log_path(config)
    if not path.exists():
        return {"downloads": {}, "scans": {}}
    log = json.loads(path.read_text(encoding="utf-8"))
    log.setdefault("downloads", {})
    log.setdefault("scans", {})
    return log


def write_log(config: Config, log: dict) -> None:
    path = log_path(config)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(log, indent=1, sort_keys=True), encoding="utf-8")


def units(config: Config) -> pd.DataFrame:
    """Return the units the forecast covers: the unit list when it is in use, otherwise the saved model's units."""
    if config.units.use_list:
        names = set(pd.read_csv(config.resolve(config.paths.unit_list))["unit"])
        return pipeline.describe_units(config, names)
    return estimator.load(config.resolve(config.paths.model_dir), "Subir")["units"]


def needed(days: list[pd.Timestamp]) -> dict[str, dict[pd.Timestamp, list[pd.Timestamp]]]:
    """Map each dataset to the days it must cover and the forecast days that use them."""
    wanted = {key: {} for key, _, _ in DATASETS}
    for day in days:
        for key, _, _ in DATASETS:
            sources = [day - pd.Timedelta(days=2), day - pd.Timedelta(days=1)] if key == "generation" else [day]
            for source in sources:
                wanted[key].setdefault(source, []).append(day)
    return {key: dict(sorted(value.items())) for key, value in wanted.items()}


def _signature(path: Path) -> list[int] | None:
    if not path.exists():
        return None
    stat = path.stat()
    return [stat.st_mtime_ns, stat.st_size]


def _scan(log: dict, name: str, path: Path, reader, extra: str = "") -> dict[str, bool]:
    """Return which days of a file are complete, re-reading the file only when it changed."""
    signature = _signature(path)
    cached = log["scans"].get(name)
    if cached and cached["signature"] == signature and cached.get("extra", "") == extra:
        return cached["days"]
    days = reader(path) if signature else {}
    log["scans"][name] = {"signature": signature, "extra": extra, "days": days}
    return days


def _by_day(frame: pd.DataFrame, rule) -> dict[str, bool]:
    return {f"{day:%Y-%m-%d}": bool(rule(rows)) for day, rows in frame.groupby("delivery_date")}


def _market(path: Path) -> dict[str, bool]:
    frame = pd.read_csv(path, parse_dates=["delivery_date"])
    columns = ["da_price", "load_forecast_mw", "wind_forecast_mw", "solar_forecast_mw"]
    return _by_day(frame, lambda rows: all(rows[c].notna().sum() >= FULL_DAY for c in columns if c in rows))


def _intraday(use_ida3: bool):
    def read(path: Path) -> dict[str, bool]:
        frame = pd.read_csv(path, parse_dates=["delivery_date"])
        for column in ("ida1_price", "ida2_price", "ida3_price"):
            if column not in frame:
                frame[column] = float("nan")

        def full(rows):
            ok = rows["ida1_price"].notna().sum() >= FULL_DAY and rows["ida2_price"].notna().sum() >= FULL_DAY
            return ok and (not use_ida3 or rows["ida3_price"].notna().sum() >= FULL_DAY // 2)
        return _by_day(frame, full)
    return read


def _totals(path: Path) -> dict[str, bool]:
    frame = pd.read_csv(path, parse_dates=["delivery_date"])
    values = [c for c in frame.columns if c not in ("delivery_date", "period")]
    return _by_day(frame, lambda rows: len(rows) >= FULL_DAY and rows[values].abs().to_numpy().sum() > 0)


def _weather(points: list[str]):
    def read(path: Path) -> dict[str, bool]:
        frame = pd.read_csv(path, usecols=["delivery_date", "point"], parse_dates=["delivery_date"])
        frame = frame[frame["point"].isin(points)]
        counts = frame.groupby(["delivery_date", "point"]).size()
        full = counts[counts >= FULL_DAY].reset_index().groupby("delivery_date")["point"].nunique()
        return {f"{day:%Y-%m-%d}": bool(n >= len(points)) for day, n in full.items()}
    return read


def _generation_periods(folder: Path, day: pd.Timestamp) -> int:
    """Return how many quarter-hours of a day the saved unit output reaches, 0 when the day is not saved."""
    path = folder / f"{day:%Y-%m-%d}.csv"
    if not path.exists():
        return 0
    periods = pd.read_csv(path, usecols=["period"])["period"]
    return int(periods.max()) + 1 if len(periods) else 0


def _at(day: pd.Timestamp, offset: int, clock: str) -> pd.Timestamp:
    """Return a Madrid time some days from a day."""
    return pd.Timestamp(f"{day + pd.Timedelta(days=offset):%Y-%m-%d} {clock}").tz_localize(TIMEZONE)


def expected(config: Config, key: str, day: pd.Timestamp, full: bool = True) -> tuple[pd.Timestamp, str]:
    """Return when a source normally publishes a dataset's data for a day, with a short note."""
    if key == "generation":
        if full:
            return _at(day, 1, "01:00"), "ENTSO-E publishes each unit's output an hour after real time; in practice often a day or more later"
        return _at(day, 0, "13:00"), "The morning output, up to noon, an hour after real time; in practice often a day or more later"
    offset, clock, note = INTRADAY[config.features.use_ida3] if key == "intraday" else PUBLISHED[key]
    return _at(day, offset, clock), note


def plan(config: Config, days: list[pd.Timestamp], now: datetime | None = None) -> dict:
    """List, per dataset and day, whether the data is on disk, still to be published, ready to download, late at the source, or final with gaps."""
    now = pd.Timestamp(now or datetime.now(timezone.utc)).tz_convert(TIMEZONE)
    log = read_log(config)
    points = sorted(pipeline.weather_spots(config, units(config))["point"])
    complete = {
        "market": _scan(log, "market", config.resolve(config.paths.market), _market),
        "intraday": _scan(log, "intraday", config.resolve(config.paths.market), _intraday(config.features.use_ida3), str(config.features.use_ida3)),
        "constraints": _scan(log, "constraints", config.resolve(config.paths.constraints), _totals),
        "schedule": _scan(log, "schedule", config.resolve(config.paths.schedule), _totals),
        "weather": _scan(log, "weather", config.resolve(config.paths.weather), _weather(points), ",".join(points)),
    }
    folder = config.resolve(config.paths.generation_dir)
    morning = config.generation.known_until_period
    result = []
    for key, title, source in DATASETS:
        rows = []
        for day, users in needed(days)[key].items():
            name = f"{day:%Y-%m-%d}"
            full = key != "generation" or any(user - day >= pd.Timedelta(days=2) for user in users)
            if key == "generation":
                have = _generation_periods(folder, day) >= (FULL_DAY if full else morning)
            else:
                have = complete[key].get(name, False)
            due, note = expected(config, key, day, full)
            downloaded = log["downloads"].get(key, {}).get(name)
            tried = pd.Timestamp(downloaded).tz_convert(TIMEZONE) if downloaded else None
            if have:
                status = "have"
            elif tried is not None and tried >= _at(day, FINAL_AFTER_DAYS, "00:00"):
                status = "gap"
            elif now < due:
                status = "pending"
            elif tried is not None and tried >= due:
                status = "late"
            else:
                status = "missing"
            retry = tried + pd.Timedelta(minutes=RETRY_MINUTES) if status == "late" else None
            rows.append({"day": name, "for": [f"{u:%Y-%m-%d}" for u in users], "status": status, "downloaded": downloaded,
                         "expected": due.isoformat(), "note": note, "full": full,
                         "retry": retry.isoformat() if retry is not None else None,
                         "fetch": status == "missing" or (status == "late" and now >= retry)})
        result.append({"key": key, "title": title, "source": source, "rows": rows})
    write_log(config, log)
    rows = [row for item in result for row in item["rows"]]
    blocked = {user for row in rows if row["status"] not in ("have", "gap") for user in row["for"]}
    timeline = {}
    for day in days:
        used = [row for row in rows if f"{day:%Y-%m-%d}" in row["for"]]
        timeline[f"{day:%Y-%m-%d}"] = {
            "start": _at(day, -2, "00:00").isoformat(), "end": _at(day, 1, "00:00").isoformat(),
            "earliest": max(pd.Timestamp(row["expected"]) for row in used).isoformat(),
        }
    span = (days[-1] - days[0]).days
    return {
        "days": [f"{day:%Y-%m-%d}" for day in days],
        "now": now.isoformat(),
        "axis": [{"day": f"{days[0] + pd.Timedelta(days=k):%Y-%m-%d}", "start": _at(days[0], k, "00:00").isoformat()} for k in range(-2, span + 1)],
        "folder": str(log_path(config).parent),
        "datasets": result,
        "missing": sum(row["fetch"] for row in rows),
        "pending": sum(row["status"] == "pending" for row in rows),
        "waiting": sum(row["status"] == "late" and not row["fetch"] for row in rows),
        "ready": [f"{day:%Y-%m-%d}" for day in days if f"{day:%Y-%m-%d}" not in blocked],
        "timeline": timeline,
        "model": model_status(config),
    }


def record(config: Config, key: str, days: list[pd.Timestamp], now: datetime | None = None) -> None:
    """Remember that these days of a dataset were downloaded now."""
    stamp = (now or datetime.now(timezone.utc)).isoformat(timespec="seconds")
    log = read_log(config)
    log["downloads"].setdefault(key, {}).update({f"{day:%Y-%m-%d}": stamp for day in days})
    write_log(config, log)


def runs(days: list[pd.Timestamp]) -> list[tuple[pd.Timestamp, pd.Timestamp]]:
    """Group days into runs of consecutive days, each as a start and an exclusive end."""
    groups = []
    for day in sorted(days):
        if groups and day == groups[-1][1]:
            groups[-1][1] = day + pd.Timedelta(days=1)
        else:
            groups.append([day, day + pd.Timedelta(days=1)])
    return [(first, last) for first, last in groups]


def model_status(config: Config) -> dict:
    """Say whether the saved models exist and use the inputs and units the current settings build."""
    folder = config.resolve(config.paths.model_dir)
    status = {"ready": True, "trained": None, "units": None, "reason": ""}
    try:
        bundles = {direction: estimator.load(folder, direction) for direction in pipeline.DIRECTION_NAMES}
    except FileNotFoundError:
        return {**status, "ready": False, "reason": "No trained models yet."}
    path = estimator.model_path(folder, "Subir")
    status["trained"] = datetime.fromtimestamp(path.stat().st_mtime, timezone.utc).isoformat(timespec="seconds")
    status["units"] = len(bundles["Subir"]["units"])
    for direction, bundle in bundles.items():
        if set(features.feature_columns(config.for_direction(direction))) - set(bundle["features"]):
            return {**status, "ready": False, "reason": "The saved models were trained with fewer inputs than the current settings build."}
    if config.units.use_list and set(bundles["Subir"]["units"]["unit"]) != set(units(config)["unit"]):
        return {**status, "ready": False, "reason": f"The saved models cover {status['units']} units, not the units in the current list."}
    return status
