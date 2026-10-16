from __future__ import annotations

import argparse
import json
import os
import sys
from datetime import datetime, timezone

import pandas as pd

from . import areas, esios, estimator, i90, ledger, locations, market, omie, pipeline, validate, weather
from .config import DEFAULT_CONFIG, Config, load_config


def _key(name: str) -> str:
    """Read an API key from the environment or exit with a clear message."""
    value = os.environ.get(name)
    if not value:
        print(f"{name} is not set", file=sys.stderr)
        sys.exit(1)
    return value


def _esios(config: Config) -> esios.Client:
    """Build the throttled e-sios client from ESIOS_API_KEY."""
    return esios.Client(_key("ESIOS_API_KEY"), config.request_interval_seconds)


def _date(text: str) -> pd.Timestamp:
    """Parse a YYYY-MM-DD argument into a normalized timestamp."""
    return pd.Timestamp(text).normalize()


def cmd_fetch_units(args: argparse.Namespace) -> None:
    """Download the unit registries and plant lists, then locate every physical unit."""
    config = load_config(args.config)
    registry_dir = config.resolve(config.paths.registry_dir)
    plants_dir = config.resolve(config.paths.plants_dir)
    if not args.skip_download:
        locations.download_registry(_esios(config), registry_dir)
        locations.download_plants(plants_dir)
        locations.download_gem(plants_dir)
        locations.download_cyl(plants_dir)
        locations.download_aragon(plants_dir)

    programming, physical = locations.load_registry(registry_dir)
    manual = pd.read_csv(config.resolve(config.paths.manual_locations))
    units = locations.locate_units(
        physical, locations.load_reference(plants_dir), manual, programming,
        locations.load_substations(plants_dir), locations.load_places(plants_dir),
    )
    path = config.resolve(config.paths.unit_locations)
    path.parent.mkdir(parents=True, exist_ok=True)
    units.to_csv(path, index=False)
    generating = units[units["technology"].map(locations.plant_sources).notna()]
    capacity = generating["max_mw"].fillna(0)
    share = capacity[generating["lat"].notna()].sum() / capacity.sum()
    print(f"unit locations: {path}")
    print(f"located {generating['lat'].notna().sum()} of {len(generating)} generating units, {share:.1%} of their capacity")


def cmd_fetch_market(args: argparse.Namespace) -> None:
    """Fetch quarter-hour ENTSO-E day-ahead prices and national forecasts."""
    config = load_config(args.config)
    prices = market.fetch_market(market.build_client(_key("ENTSOE_API_KEY")), _date(args.start), _date(args.end))
    frame = market.upsert(config.resolve(config.paths.market), prices)
    print(f"market: {len(prices)} new rows, {len(frame)} total")


def cmd_fetch_intraday(args: argparse.Namespace) -> None:
    """Fetch quarter-hour Spanish IDA1-3 prices from OMIE and merge them into the market file."""
    config = load_config(args.config)
    days = [d.date() for d in pd.date_range(_date(args.start), _date(args.end) - pd.Timedelta(days=1), freq="D")]
    prices = omie.fetch_intraday(days)
    target = config.resolve(config.paths.market)
    target.parent.mkdir(parents=True, exist_ok=True)
    prices.to_csv(target.with_name(f"intraday_{args.start}_{args.end}.csv"), index=False)
    frame = market.upsert(target, prices)
    print(f"intraday: {len(prices)} new rows, {len(frame)} total")


def cmd_fetch_weather(args: argparse.Namespace) -> None:
    """Fetch Open-Meteo forecasts for the weather grid covering every congestion cell, at quarter-hours."""
    config = load_config(args.config)
    units = pd.read_csv(config.resolve(config.paths.unit_locations))
    capacity = areas.capacity_by_area(units, config.areas.cell_degrees, config.areas.min_located_mw)
    _, centres = weather.points_for(capacity, config.weather.grid_degrees)
    today = pd.Timestamp(datetime.now(timezone.utc).date())
    frame = weather.fetch(
        centres, _date(args.start), _date(args.end), today,
        per_request=config.weather.points_per_request,
        interval_seconds=config.weather.request_interval_seconds,
    )
    merged = market.upsert(config.resolve(config.paths.weather), frame, key=weather.KEY)
    print(f"weather: {len(centres)} points, {len(frame)} new rows, {len(merged)} total")


def cmd_fetch_constraints(args: argparse.Namespace) -> None:
    """Fetch REE's national day-ahead constraint totals per quarter-hour."""
    config = load_config(args.config)
    totals = market.fetch_constraints(_esios(config), config.constraint_indicators, _date(args.start), _date(args.end))
    frame = market.upsert(config.resolve(config.paths.constraints), totals)
    print(f"constraints: {len(totals)} new rows, {len(frame)} total")


def cmd_fetch_redispatch(args: argparse.Namespace) -> None:
    """Download per-unit redispatch (I90DIA) for each day in a range, skipping cached days."""
    config = load_config(args.config)
    days = [d.date() for d in pd.date_range(_date(args.start), _date(args.end) - pd.Timedelta(days=1), freq="D")]
    stats = i90.download(_esios(config), days, config.resolve(config.paths.redispatch_dir))
    print(json.dumps(stats))


def cmd_train(args: argparse.Namespace) -> None:
    """Build the dataset from history and save a trained model."""
    config = load_config(args.config)
    inputs = pipeline.load_inputs(config)
    dataset = pipeline.build_training(config, inputs, pipeline.label_cutoff(config))
    bundle, report = pipeline.train(config, inputs, dataset)
    path = estimator.save(bundle, config.resolve(config.paths.model_dir))
    print(json.dumps(report, indent=2, default=str))
    print(f"saved: {path}")


def cmd_predict(args: argparse.Namespace) -> None:
    """Issue area predictions for one delivery day and log them."""
    config = load_config(args.config)
    bundle = estimator.load(config.resolve(config.paths.model_dir))
    inputs = pipeline.load_inputs(config)
    day = _date(args.date)
    dataset = pipeline.build(config, inputs, None, bundle["area_codes"], [day])
    issued_at = datetime.fromisoformat(args.issued_at) if args.issued_at else None
    predictions = pipeline.predict_day(bundle, dataset, day, issued_at)
    path = config.resolve(config.paths.predictions)
    ledger.append(predictions, path)

    top = (predictions.groupby("area")["risk_probability"].max().sort_values(ascending=False).head(args.top))
    size = bundle["cell_degrees"]
    print(f"{len(predictions)} rows for {day:%Y-%m-%d}; highest-risk cells (centre lat, lon, max 15-minute probability):")
    for area, probability in top.items():
        lat, lon = areas.cell_center(area, size)
        print(f"  {area}  ({lat:.2f}, {lon:.2f})  {probability:.2f}")
    print(f"logged: {path}")


def cmd_validate(args: argparse.Namespace) -> None:
    """Score logged predictions against the per-unit redispatch REE has published so far."""
    config = load_config(args.config)
    as_of = _date(args.as_of) if args.as_of else pd.Timestamp(datetime.now(timezone.utc).date())
    label_end = as_of - pd.Timedelta(days=config.labels.settle_days)
    predictions = ledger.load(config.resolve(config.paths.predictions))
    result = validate.evaluate(config, predictions, pipeline.load_inputs(config), label_end, as_of)
    history = ledger.append_history(result, config.resolve(config.paths.validation))
    print(json.dumps(result, indent=2, default=str))
    print(f"history rows: {len(history)}")
    if result["alert"]:
        print("ALERT: window skill is below the configured minimum", file=sys.stderr)
        sys.exit(2)


def build_parser() -> argparse.ArgumentParser:
    """Create the argument parser with one subcommand per workflow step."""
    parser = argparse.ArgumentParser(prog="congestion-es")
    parser.add_argument("--config", default=str(DEFAULT_CONFIG), help="path to config.toml")
    sub = parser.add_subparsers(dest="command", required=True)

    units = sub.add_parser("fetch-units", help="download unit registries and plant lists, locate units")
    units.add_argument("--skip-download", action="store_true", help="reuse the registry and plant files on disk")
    units.set_defaults(func=cmd_fetch_units)

    for name, func, text in (
        ("fetch-market", cmd_fetch_market, "fetch ENTSO-E day-ahead prices and forecasts"),
        ("fetch-intraday", cmd_fetch_intraday, "fetch OMIE IDA1-3 prices"),
        ("fetch-weather", cmd_fetch_weather, "fetch Open-Meteo regional weather forecasts"),
        ("fetch-constraints", cmd_fetch_constraints, "fetch REE national constraint totals"),
        ("fetch-redispatch", cmd_fetch_redispatch, "download per-unit redispatch files (I90DIA)"),
    ):
        command = sub.add_parser(name, help=text)
        command.add_argument("--start", required=True, help="first delivery date YYYY-MM-DD")
        command.add_argument("--end", required=True, help="day after the last delivery date YYYY-MM-DD")
        command.set_defaults(func=func)

    sub.add_parser("train", help="train and save the estimator").set_defaults(func=cmd_train)

    predict = sub.add_parser("predict", help="issue area predictions for a delivery day")
    predict.add_argument("--date", required=True, help="delivery date YYYY-MM-DD")
    predict.add_argument("--issued-at", help="ISO timestamp recorded as issue time")
    predict.add_argument("--top", type=int, default=10, help="number of highest-risk cells to print")
    predict.set_defaults(func=cmd_predict)

    check = sub.add_parser("validate", help="score predictions against published redispatch")
    check.add_argument("--as-of", help="evaluation date YYYY-MM-DD, default today")
    check.set_defaults(func=cmd_validate)
    return parser


def main(argv: list[str] | None = None) -> None:
    """Parse arguments and run the selected command."""
    args = build_parser().parse_args(argv)
    args.func(args)
