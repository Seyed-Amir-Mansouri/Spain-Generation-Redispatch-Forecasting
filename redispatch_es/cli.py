from __future__ import annotations

import argparse
import dataclasses
import json
import os
import sys
import time
from datetime import datetime, timezone

import pandas as pd

from . import esios, estimator, generation, i90, inventory, ledger, locations, market, metrics, omie, pipeline, validate, weather
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


def _days(start: pd.Timestamp, end: pd.Timestamp) -> list[pd.Timestamp]:
    """List the delivery days from start up to the exclusive end."""
    return list(pd.date_range(start, end - pd.Timedelta(days=1), freq="D"))


def fetch_market(config: Config, start: pd.Timestamp, end: pd.Timestamp) -> None:
    """Fetch quarter-hour ENTSO-E day-ahead prices and national forecasts."""
    prices = market.fetch_market(market.build_client(_key("ENTSOE_API_KEY")), start, end)
    frame = market.upsert(config.resolve(config.paths.market), prices)
    print(f"market: {len(prices)} new rows, {len(frame)} total")


def fetch_intraday(config: Config, start: pd.Timestamp, end: pd.Timestamp) -> None:
    """Fetch quarter-hour Spanish IDA1-3 prices from OMIE and merge them into the market file."""
    prices = omie.fetch_intraday([d.date() for d in _days(start, end)])
    target = config.resolve(config.paths.market)
    target.parent.mkdir(parents=True, exist_ok=True)
    prices.to_csv(target.with_name(f"intraday_{start:%Y-%m-%d}_{end:%Y-%m-%d}.csv"), index=False)
    frame = market.upsert(target, prices)
    print(f"intraday: {len(prices)} new rows, {len(frame)} total")


def fetch_weather(config: Config, start: pd.Timestamp, end: pd.Timestamp, units: pd.DataFrame | None = None) -> None:
    """Fetch Open-Meteo forecasts at the spots of the predicted wind and solar units, at quarter-hours."""
    centres = pipeline.weather_spots(config, pipeline.select_units(config) if units is None else units)
    today = pd.Timestamp(datetime.now(timezone.utc).date())
    frame = weather.fetch(
        centres, start, end, today,
        per_request=config.weather.points_per_request,
        interval_seconds=config.weather.request_interval_seconds,
        calls_per_hour=config.weather.calls_per_hour,
    )
    merged = market.upsert(config.resolve(config.paths.weather), frame, key=weather.KEY)
    print(f"weather: {len(centres)} points, {len(frame)} new rows, {len(merged)} total")


def fetch_constraints(config: Config, start: pd.Timestamp, end: pd.Timestamp) -> None:
    """Fetch REE's national day-ahead constraint totals per quarter-hour."""
    totals = market.fetch_constraints(_esios(config), config.constraint_indicators, start, end)
    frame = market.upsert(config.resolve(config.paths.constraints), totals)
    print(f"constraints: {len(totals)} new rows, {len(frame)} total")


def fetch_generation(config: Config, start: pd.Timestamp, end: pd.Timestamp) -> None:
    """Download ENTSO-E actual output per unit, one file per delivery day, skipping cached days."""
    plants = pd.read_csv(config.resolve(config.paths.generation_plants))
    stats = generation.fetch(
        generation.build_client(_key("ENTSOE_API_KEY")), _days(start, end), config.resolve(config.paths.generation_dir),
        dict(zip(plants["plant_eic"], plants["eic"])), config.generation.request_interval_seconds,
    )
    print(f"generation: {json.dumps(stats)}")


def fetch_schedule(config: Config, start: pd.Timestamp, end: pd.Timestamp) -> None:
    """Fetch REE's national day-ahead schedule per technology per quarter-hour."""
    schedule = market.fetch_constraints(_esios(config), config.schedule_indicators, start, end)
    frame = market.upsert(config.resolve(config.paths.schedule), schedule)
    print(f"schedule: {len(schedule)} new rows, {len(frame)} total")


def _range_command(fetch):
    def command(args: argparse.Namespace) -> None:
        fetch(load_config(args.config), _date(args.start), _date(args.end))
    command.__doc__ = fetch.__doc__
    return command


cmd_fetch_market = _range_command(fetch_market)
cmd_fetch_intraday = _range_command(fetch_intraday)
cmd_fetch_weather = _range_command(fetch_weather)
cmd_fetch_constraints = _range_command(fetch_constraints)
cmd_fetch_generation = _range_command(fetch_generation)
cmd_fetch_schedule = _range_command(fetch_schedule)


def _plan_days(args: argparse.Namespace) -> list[pd.Timestamp]:
    end = _date(args.end) if args.end else _date(args.start) + pd.Timedelta(days=1)
    return _days(_date(args.start), end)


def cmd_plan(args: argparse.Namespace) -> None:
    """Show which inputs the forecast days need and which are already downloaded."""
    config = load_config(args.config)
    result = inventory.plan(config, _plan_days(args))
    if args.json:
        print(json.dumps(result))
        return
    for item in result["datasets"]:
        print(f"{item['title']} ({item['source']}): " + ", ".join(f"{row['day']} {row['status']}" for row in item["rows"]))
    print(f"{result['missing']} downloads needed; ready to forecast: {', '.join(result['ready']) or 'none'}")
    if not result["model"]["ready"]:
        print(f"models: {result['model']['reason']} Run train before predict.")


def _progress(**fields) -> None:
    print("PROGRESS " + json.dumps(fields), flush=True)


def cmd_fetch_days(args: argparse.Namespace) -> None:
    """Download only the inputs the forecast days still miss, one source at a time, and remember what was downloaded."""
    config = load_config(args.config)
    days = _plan_days(args)
    result = inventory.plan(config, days)
    fetchers = {
        "market": fetch_market, "intraday": fetch_intraday, "constraints": fetch_constraints,
        "schedule": fetch_schedule, "generation": fetch_generation,
        "weather": lambda c, a, b: fetch_weather(c, a, b, inventory.units(c)),
    }
    folder = config.resolve(config.paths.generation_dir)
    asked = {}
    for item in result["datasets"]:
        missing = [_date(row["day"]) for row in item["rows"] if row["fetch"]]
        pending = sum(row["status"] == "pending" for row in item["rows"])
        waiting = sum(row["status"] == "late" and not row["fetch"] for row in item["rows"])
        if not missing:
            state = "not_published" if pending else "retry_later" if waiting else "on_disk"
            _progress(dataset=item["key"], state=state, pending=pending, waiting=waiting)
            continue
        _progress(dataset=item["key"], state="running", days=len(missing), pending=pending)
        if item["key"] == "generation":
            for day in missing:
                (folder / f"{day:%Y-%m-%d}.csv").unlink(missing_ok=True)
        try:
            for first, last in inventory.runs(missing):
                if item["source"] == "REE e-sios":
                    time.sleep(config.request_interval_seconds)
                fetchers[item["key"]](config, first, last)
        except Exception as error:
            print(f"{item['key']}: {error}", flush=True)
            _progress(dataset=item["key"], state="failed", error=str(error))
            continue
        inventory.record(config, item["key"], missing)
        asked[item["key"]] = ({f"{day:%Y-%m-%d}" for day in missing}, pending)
        _progress(dataset=item["key"], state="checking", pending=pending)
    final = inventory.plan(config, days)
    for item in final["datasets"]:
        if item["key"] in asked:
            names, pending = asked[item["key"]]
            late = sum(row["day"] in names and row["status"] not in ("have", "gap") for row in item["rows"])
            _progress(dataset=item["key"], state="late" if late else "done", late=late, pending=pending)
    open_items = sum(row["status"] not in ("have", "gap") for item in final["datasets"] for row in item["rows"])
    _progress(state="finished", missing=open_items, ready=final["ready"])
    print(f"{open_items} inputs not on disk yet; ready to forecast: {', '.join(final['ready']) or 'none'}")


def cmd_fetch_redispatch(args: argparse.Namespace) -> None:
    """Download per-unit redispatch (I90DIA) for each day in a range, skipping cached days."""
    config = load_config(args.config)
    days = [d.date() for d in pd.date_range(_date(args.start), _date(args.end) - pd.Timedelta(days=1), freq="D")]
    stats = i90.download(_esios(config), days, config.resolve(config.paths.redispatch_dir))
    print(json.dumps(stats))


def _directions(config: Config, args: argparse.Namespace) -> list[Config]:
    """Return one config per redispatch direction, or only the one asked for."""
    chosen = [args.direction] if getattr(args, "direction", None) else config.labels.directions
    return [config.for_direction(direction) for direction in chosen]


def cmd_train(args: argparse.Namespace) -> None:
    """Pick the units, build each direction's dataset from history, save its model, and score the up/down/nothing calls."""
    base = load_config(args.config)
    if args.test_days is not None:
        base = dataclasses.replace(base, model=dataclasses.replace(base.model, test_days=args.test_days))
    units = pipeline.select_units(base)
    holdouts = {}
    for config in _directions(base, args):
        inputs = pipeline.load_inputs(config, units)
        dataset = pipeline.build_training(config, inputs, pipeline.label_cutoff(config))
        bundle, report, holdouts[config.labels.direction] = pipeline.train(config, inputs, dataset)
        path = estimator.save(bundle, config.resolve(config.paths.model_dir))
        del dataset
        print(json.dumps(report, indent=2, default=str))
        print(f"saved: {path}")
    if all(holdouts.get(direction) is not None for direction in pipeline.DIRECTION_NAMES):
        decisions = pipeline.holdout_decisions(holdouts, base.model.threshold)
        print(json.dumps({"holdout_decisions": metrics.decision_counts(decisions)}, indent=2, default=str))


def cmd_backtest(args: argparse.Namespace) -> None:
    """Predict each of the last days with published redispatch with models trained on every earlier day, and print the result tables."""
    config = load_config(args.config)
    decisions = pipeline.backtest(config, pipeline.select_units(config), args.days)
    path = config.resolve(config.paths.backtest)
    path.parent.mkdir(parents=True, exist_ok=True)
    decisions.to_csv(path, index=False)
    days = sorted(decisions["delivery_date"].unique())
    rows = {f"{day:%d %b %Y}": decisions[decisions["delivery_date"] == day] for day in days}
    if len(days) > 1:
        rows[f"All {len(days)} days"] = decisions
    title = f"{decisions['unit'].nunique()} units, each day trained up to the day before, threshold {config.model.threshold}"
    print(metrics.result_tables(rows, title))
    print(f"calls: {path}")


def cmd_predict(args: argparse.Namespace) -> None:
    """Call each unit up, down or nothing for every quarter-hour of one delivery day, or a range of days, and log the calls."""
    config = load_config(args.config)
    first = _date(args.date)
    days = _days(first, _date(args.end)) if args.end else [first]
    issued_at = datetime.fromisoformat(args.issued_at) if args.issued_at else None
    model_dir = config.resolve(config.paths.model_dir)
    bundles = {direction: estimator.load(model_dir, direction) for direction in pipeline.DIRECTION_NAMES}
    units = bundles["Bajar"]["units"]
    if not units.equals(bundles["Subir"]["units"]):
        print("the downward and upward models were trained on different units; run train for both directions", file=sys.stderr)
        sys.exit(1)
    inputs = pipeline.load_inputs(config, units, bundles["Bajar"].get("routine"))
    dataset = pipeline.build(config, inputs, None, days)
    path = config.resolve(config.paths.predictions)
    failed = 0
    for day in days:
        try:
            predictions = pipeline.predict_day(bundles, dataset, day, config.model.threshold, issued_at)
        except ValueError as error:
            print(error, flush=True)
            failed += 1
            continue
        ledger.append(predictions, path)
        calls = predictions[predictions["decision"] != "nothing"]
        print(f"{len(predictions)} rows for {day:%Y-%m-%d}: "
              + ", ".join(f"{name} {(predictions['decision'] == name).sum():,}" for name in ("down", "up", "nothing")), flush=True)
        if len(days) == 1:
            print("units with the most up or down quarter-hours:")
            for (unit, decision), count in calls.groupby(["unit", "decision"]).size().sort_values(ascending=False).head(args.top).items():
                print(f"  {unit}  {decision}  {count} quarter-hours")
    print(f"logged: {path}")
    if failed == len(days):
        sys.exit(1)


def cmd_validate(args: argparse.Namespace) -> None:
    """Score the logged up/down/nothing calls against the per-unit redispatch REE has published so far."""
    config = load_config(args.config)
    as_of = _date(args.as_of) if args.as_of else pd.Timestamp(datetime.now(timezone.utc).date())
    label_end = as_of - pd.Timedelta(days=config.labels.settle_days)
    predictions = ledger.load(config.resolve(config.paths.predictions))
    units = pd.DataFrame({"unit": sorted(predictions["unit"].unique())})
    inputs = {direction: pipeline.load_inputs(config.for_direction(direction), units) for direction in pipeline.DIRECTION_NAMES}
    result = validate.evaluate(config, predictions, inputs, label_end, as_of)
    history = ledger.append_history(result, config.resolve(config.paths.validation))
    print(json.dumps(result, indent=2, default=str))
    print(f"history rows: {len(history)}")
    if result["alert"]:
        print("ALERT: window skill is below the configured minimum", file=sys.stderr)
        sys.exit(2)


def build_parser() -> argparse.ArgumentParser:
    """Create the argument parser with one subcommand per workflow step."""
    parser = argparse.ArgumentParser(prog="redispatch-es")
    parser.add_argument("--config", default=str(DEFAULT_CONFIG), help="path to config.toml")
    sub = parser.add_subparsers(dest="command", required=True)

    units = sub.add_parser("fetch-units", help="download unit registries and plant lists, locate units")
    units.add_argument("--skip-download", action="store_true", help="reuse the registry and plant files on disk")
    units.set_defaults(func=cmd_fetch_units)

    for name, func, text in (
        ("fetch-market", cmd_fetch_market, "fetch ENTSO-E day-ahead prices and forecasts"),
        ("fetch-intraday", cmd_fetch_intraday, "fetch OMIE IDA1-3 prices"),
        ("fetch-weather", cmd_fetch_weather, "fetch Open-Meteo weather at the wind and solar spots"),
        ("fetch-constraints", cmd_fetch_constraints, "fetch REE national constraint totals"),
        ("fetch-schedule", cmd_fetch_schedule, "fetch REE's national day-ahead schedule per technology"),
        ("fetch-generation", cmd_fetch_generation, "download ENTSO-E actual output per unit"),
        ("fetch-redispatch", cmd_fetch_redispatch, "download per-unit redispatch files (I90DIA)"),
    ):
        command = sub.add_parser(name, help=text)
        command.add_argument("--start", required=True, help="first delivery date YYYY-MM-DD")
        command.add_argument("--end", required=True, help="day after the last delivery date YYYY-MM-DD")
        command.set_defaults(func=func)

    for name, func, text in (
        ("plan", cmd_plan, "show which inputs the forecast days need and which are downloaded"),
        ("fetch-days", cmd_fetch_days, "download only the inputs the forecast days still miss"),
    ):
        command = sub.add_parser(name, help=text)
        command.add_argument("--start", required=True, help="first forecast day YYYY-MM-DD")
        command.add_argument("--end", help="day after the last forecast day YYYY-MM-DD; default one day")
        if name == "plan":
            command.add_argument("--json", action="store_true", help="print the plan as JSON")
        command.set_defaults(func=func)

    train = sub.add_parser("train", help="train and save the models for each direction")
    train.add_argument("--direction", help="train only this direction, e.g. Bajar or Subir")
    train.add_argument("--test-days", type=int, help="score the last N days with published redispatch before the final refit; default test_days in config, 0 for none")
    train.set_defaults(func=cmd_train)

    backtest = sub.add_parser("backtest", help="predict each of the last days with published redispatch, each trained up to the day before")
    backtest.add_argument("--days", type=int, default=1, help="number of last days with published redispatch to predict")
    backtest.set_defaults(func=cmd_backtest)

    predict = sub.add_parser("predict", help="call each unit up, down or nothing for a delivery day")
    predict.add_argument("--date", required=True, help="delivery date YYYY-MM-DD, or the first of a range")
    predict.add_argument("--end", help="day after the last delivery date of a range YYYY-MM-DD")
    predict.add_argument("--issued-at", help="ISO timestamp recorded as issue time")
    predict.add_argument("--top", type=int, default=10, help="number of units with the most up or down quarter-hours to print")
    predict.set_defaults(func=cmd_predict)

    check = sub.add_parser("validate", help="score the up/down/nothing calls against published redispatch")
    check.add_argument("--as-of", help="evaluation date YYYY-MM-DD, default today")
    check.set_defaults(func=cmd_validate)
    return parser


def main(argv: list[str] | None = None) -> None:
    """Parse arguments and run the selected command."""
    args = build_parser().parse_args(argv)
    args.func(args)
