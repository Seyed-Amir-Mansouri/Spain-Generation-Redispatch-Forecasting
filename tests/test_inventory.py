import dataclasses
from datetime import datetime, timezone

import pandas as pd

from redispatch_es import inventory


def _day(text):
    return pd.Timestamp(text)


def _quarters(day, **columns):
    return pd.DataFrame({"delivery_date": day, "period": range(96), **columns})


def _setup(config, tmp_path, monkeypatch):
    config = dataclasses.replace(config, root=tmp_path)
    monkeypatch.setattr(inventory, "units", lambda c: pd.DataFrame({"unit": ["A"]}))
    monkeypatch.setattr(inventory.pipeline, "weather_spots", lambda c, u: pd.DataFrame({"point": ["p1"]}))
    monkeypatch.setattr(inventory, "model_status", lambda c: {"ready": True})
    raw = tmp_path / "data" / "raw"
    raw.mkdir(parents=True)
    full = dict(da_price=1.0, load_forecast_mw=1.0, wind_forecast_mw=1.0, solar_forecast_mw=1.0, ida1_price=1.0, ida2_price=1.0, ida3_price=1.0)
    partial = dict(full, wind_forecast_mw=None, ida3_price=None)
    pd.concat([_quarters("2026-10-01", **full), _quarters("2026-10-02", **partial)]).to_csv(config.resolve(config.paths.market), index=False)
    pd.concat([_quarters("2026-10-01", a=5.0), _quarters("2026-10-02", a=0.0)]).to_csv(config.resolve(config.paths.constraints), index=False)
    _quarters("2026-10-01", a=5.0).to_csv(config.resolve(config.paths.schedule), index=False)
    _quarters("2026-10-01", point="p1", wind_speed_100m=1.0).to_csv(config.resolve(config.paths.weather), index=False)
    folder = config.resolve(config.paths.generation_dir)
    folder.mkdir(parents=True)
    pd.DataFrame({"delivery_date": "2026-09-29", "period": range(96), "eic": "x", "mw": 1.0}).to_csv(folder / "2026-09-29.csv", index=False)
    pd.DataFrame({"delivery_date": "2026-09-30", "period": range(50), "eic": "x", "mw": 1.0}).to_csv(folder / "2026-09-30.csv", index=False)
    return config


def _statuses(result):
    return {item["key"]: {row["day"]: row["status"] for row in item["rows"]} for item in result["datasets"]}


def test_needed_asks_for_unit_output_of_the_two_days_before():
    wanted = inventory.needed([_day("2026-10-02")])
    assert list(wanted["generation"]) == [_day("2026-09-30"), _day("2026-10-01")]
    assert list(wanted["market"]) == [_day("2026-10-02")]


def test_runs_groups_consecutive_days():
    days = [_day("2026-10-05"), _day("2026-10-01"), _day("2026-10-02")]
    assert inventory.runs(days) == [(_day("2026-10-01"), _day("2026-10-03")), (_day("2026-10-05"), _day("2026-10-06"))]


def test_plan_marks_complete_days_and_misses_partial_ones(config, tmp_path, monkeypatch):
    config = _setup(config, tmp_path, monkeypatch)
    result = inventory.plan(config, [_day("2026-10-01"), _day("2026-10-02")])
    status = _statuses(result)
    assert status["market"] == {"2026-10-01": "have", "2026-10-02": "missing"}
    assert status["intraday"] == {"2026-10-01": "have", "2026-10-02": "missing"}
    assert status["constraints"] == {"2026-10-01": "have", "2026-10-02": "missing"}
    assert status["weather"] == {"2026-10-01": "have", "2026-10-02": "missing"}
    assert status["generation"] == {"2026-09-29": "have", "2026-09-30": "missing", "2026-10-01": "missing"}
    assert result["ready"] == []
    assert inventory.log_path(config).exists()


def test_unit_output_up_to_noon_is_enough_for_the_day_before(config, tmp_path, monkeypatch):
    config = _setup(config, tmp_path, monkeypatch)
    status = _statuses(inventory.plan(config, [_day("2026-10-01")]))
    assert status["generation"] == {"2026-09-29": "have", "2026-09-30": "have"}


def test_a_day_downloaded_long_after_it_ended_is_not_downloaded_again(config, tmp_path, monkeypatch):
    config = _setup(config, tmp_path, monkeypatch)
    inventory.record(config, "market", [_day("2026-10-02")], now=datetime(2026, 10, 3, tzinfo=timezone.utc))
    assert _statuses(inventory.plan(config, [_day("2026-10-02")]))["market"]["2026-10-02"] == "late"
    inventory.record(config, "market", [_day("2026-10-02")], now=datetime(2026, 10, 9, tzinfo=timezone.utc))
    assert _statuses(inventory.plan(config, [_day("2026-10-02")]))["market"]["2026-10-02"] == "gap"


def test_a_late_item_is_asked_again_only_after_a_pause(config, tmp_path, monkeypatch):
    config = _setup(config, tmp_path, monkeypatch)
    inventory.record(config, "market", [_day("2026-10-02")], now=datetime(2026, 10, 2, 9, 0, tzinfo=timezone.utc))
    row = lambda result: next(r for d in result["datasets"] if d["key"] == "market" for r in d["rows"])
    soon = inventory.plan(config, [_day("2026-10-02")], now=datetime(2026, 10, 2, 9, 10, tzinfo=timezone.utc))
    assert row(soon)["status"] == "late" and not row(soon)["fetch"] and soon["waiting"] >= 1
    later = inventory.plan(config, [_day("2026-10-02")], now=datetime(2026, 10, 2, 9, 40, tzinfo=timezone.utc))
    assert row(later)["status"] == "late" and row(later)["fetch"]


def test_a_day_not_published_yet_is_pending(config, tmp_path, monkeypatch):
    config = _setup(config, tmp_path, monkeypatch)
    now = datetime(2026, 10, 1, 9, 0, tzinfo=timezone.utc)
    result = inventory.plan(config, [_day("2026-10-02")], now=now)
    status = _statuses(result)
    assert status["market"]["2026-10-02"] == "pending"
    assert status["intraday"]["2026-10-02"] == "pending"
    assert status["weather"]["2026-10-02"] == "missing"
    assert result["pending"] > 0 and result["ready"] == []
    assert result["timeline"]["2026-10-02"]["earliest"].startswith("2026-10-02T10:30")
