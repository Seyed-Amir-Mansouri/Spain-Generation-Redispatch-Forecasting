import dataclasses
from datetime import datetime, timezone

import numpy as np
import pandas as pd

from congestion_es import estimator, ledger, locations, metrics, pipeline, validate, weather
from congestion_es.config import DEFAULT_CONFIG, load_config
from congestion_es.features import feature_columns

UNITS = ["EOLA", "FVB", "CCGC"]


def _inputs(config, direction="Bajar", days=200, seed=5):
    """Synthetic inputs where EOLA and FVB go down with the national downward total and CCGC goes up with the upward one."""
    rng = np.random.default_rng(seed)
    dates = pd.date_range("2025-06-01", periods=days, freq="D")
    national = pd.MultiIndex.from_product([dates, range(96)], names=["delivery_date", "period"]).to_frame(index=False)
    n = len(national)
    market = national.assign(
        da_price=rng.normal(60, 20, n), ida1_price=rng.normal(60, 20, n), ida2_price=rng.normal(60, 20, n),
        ida3_price=rng.normal(60, 20, n), load_forecast_mw=rng.normal(28000, 3000, n),
        wind_forecast_mw=rng.gamma(2, 3000, n), solar_forecast_mw=rng.gamma(2, 4000, n),
    )
    down, up = rng.gamma(1.5, 300, n), rng.gamma(1.5, 300, n)
    constraints = national.copy()
    for name in config.constraint_indicators:
        constraints[name] = rng.gamma(1.5, 100, n)
    constraints["down_overload_contingency"] = down
    constraints["up_overload_contingency"] = up

    units = pd.DataFrame({"unit": UNITS, "unit_id": [0, 1, 2], "technology_id": [2, 0, 3]})
    places = pd.DataFrame({"unit": UNITS, "lat": [37.7, 38.7, 41.2], "lon": [-5.7, -6.2, -0.7]})

    rows = []
    for (day, period), low, high in zip(national[["delivery_date", "period"]].itertuples(index=False), down, up):
        if direction == "Bajar" and low > 500:
            rows.append((day, period, UNITS[0], low / 10))
        if direction == "Bajar" and low > 900:
            rows.append((day, period, UNITS[1], low / 20))
        if direction == "Subir" and high > 500:
            rows.append((day, period, UNITS[2], high / 10))
    redispatch = pd.DataFrame(rows, columns=["delivery_date", "period", "unit", "mwh"])
    points, _ = weather.area_weather(places, weather.spots_for(places, config.weather.spot_degrees), pd.DataFrame(columns=weather.KEY + list(weather.VARIABLES)), config.weather.area_degrees)
    forecast = national.merge(points[["point"]].drop_duplicates(), how="cross")
    m = len(forecast)
    forecast = forecast.assign(
        wind_speed_100m=rng.gamma(2, 10, m), shortwave_radiation=rng.uniform(0, 900, m), cloud_cover=rng.uniform(0, 100, m)
    )
    return pipeline.Inputs(
        market=market, constraints=constraints, units=units, weather=forecast, points=points, redispatch=redispatch,
        label_days=set(dates[: days - 20]), coverage=0.7,
    )


def _trained(config, label_end=pd.Timestamp("2025-12-17")):
    """Train both directions on the synthetic inputs and return their inputs, bundles and holdouts."""
    inputs, bundles, holdouts = {}, {}, {}
    for direction in pipeline.DIRECTION_NAMES:
        inputs[direction] = _inputs(config, direction)
        dataset = pipeline.build(config, inputs[direction], label_end)
        bundles[direction], _, holdouts[direction] = pipeline.train(config.for_direction(direction), inputs[direction], dataset)
    return inputs, bundles, holdouts


def test_target_is_unknown_for_missing_days_and_after_label_end(config):
    inputs = _inputs(config, days=40)
    dataset = pipeline.build(config, inputs, pd.Timestamp("2025-06-25"))
    dates = dataset["delivery_date"]
    assert dataset.loc[dates <= pd.Timestamp("2025-06-20"), "target"].notna().all()
    assert dataset.loc[dates.between(pd.Timestamp("2025-06-21"), pd.Timestamp("2025-06-25")), "target"].isna().all()
    assert dataset.loc[dates > pd.Timestamp("2025-06-25"), "target"].isna().all()


def test_train_and_predict_call_each_unit_up_down_or_nothing(config):
    inputs, bundles, holdouts = _trained(config)
    assert bundles["Bajar"]["features"] == feature_columns(config)
    assert bundles["Subir"]["direction"] == "Subir"

    decisions = pipeline.holdout_decisions(holdouts, config.model.threshold)
    counts = metrics.decision_counts(decisions)
    assert counts["down_recall"] > 0.5 and counts["up_recall"] > 0.5
    assert counts["down_wrong_direction"] + counts["up_wrong_direction"] == 0

    day = pd.Timestamp("2025-12-10")
    features = pipeline.build(config, inputs["Bajar"], None)
    predictions = pipeline.predict_day(bundles, features, day, config.model.threshold, datetime(2025, 12, 9, 12, tzinfo=timezone.utc))
    assert list(predictions.columns) == ledger.PREDICTION_COLUMNS
    assert len(predictions) == 96 * len(UNITS)
    assert set(predictions["decision"]) <= {"down", "up", "nothing"}
    calls = predictions.groupby(["unit", "decision"]).size()
    assert calls.get(("CCGC", "down"), 0) == 0 and calls.get(("EOLA", "up"), 0) == 0
    assert calls.get(("EOLA", "down"), 0) > calls.get(("FVB", "down"), 0)
    assert calls.get(("CCGC", "up"), 0) > 0


def test_training_build_samples_negatives_but_keeps_holdout_whole(config):
    inputs = _inputs(config)
    label_end = pd.Timestamp("2025-12-17")
    full = pipeline.build(config, inputs, label_end)
    full = full[full["target"].notna()]
    sampled = pipeline.build_training(config, inputs, label_end)
    cutoff = full["delivery_date"].max() - pd.Timedelta(days=config.model.test_days)
    early, late = sampled["delivery_date"] <= cutoff, sampled["delivery_date"] > cutoff
    assert (sampled["target"] == 1).sum() == (full["target"] == 1).sum()
    assert late.sum() == (full["delivery_date"] > cutoff).sum()
    assert sampled.loc[early & (sampled["target"] == 0), "weight"].eq(1 / config.model.negative_fraction).all()
    assert abs(sampled["weight"].sum() - len(full)) < 0.05 * len(full)
    bundle, report, holdout = pipeline.train(config, inputs, sampled)
    assert report["holdout"]["n"] == late.sum() == len(holdout)


def test_models_are_saved_per_direction(config, tmp_path):
    _, bundles, _ = _trained(config)
    path = estimator.save(bundles["Subir"], tmp_path)
    assert path.name == "redispatch_model_subir.joblib"
    loaded = estimator.load(tmp_path, "Subir")
    assert loaded["model_version"].startswith("lgbm-")
    assert loaded["units"]["unit"].tolist() == UNITS


def test_validation_counts_up_down_and_nothing_calls(config):
    inputs, bundles, _ = _trained(config)
    features = pipeline.build(config, inputs["Bajar"], None)
    issued = datetime(2025, 11, 1, tzinfo=timezone.utc)
    days = pd.date_range("2025-11-01", "2025-11-10")
    predictions = pd.concat([pipeline.predict_day(bundles, features, day, config.model.threshold, issued) for day in days], ignore_index=True)
    predictions["issued_at"] = pd.to_datetime(predictions["issued_at"], utc=True)
    result = validate.evaluate(config, predictions, inputs, pd.Timestamp("2025-11-10"), pd.Timestamp("2026-02-10"))
    assert result["overall_n"] == len(days) * 96 * len(UNITS)
    assert result["overall_down_happened"] > 0 and result["overall_up_happened"] > 0
    assert result["overall_down_recall"] > 0.5 and result["overall_up_recall"] > 0.5
    assert result["overall_accuracy"] > 0.8


def test_decide_picks_the_more_likely_direction_above_the_threshold():
    decisions = metrics.decide([0.9, 0.2, 0.6, 0.3], [0.1, 0.7, 0.55, 0.4], 0.5)
    assert decisions.tolist() == ["down", "up", "down", "nothing"]


def test_decision_counts_split_right_missed_wrong_direction_and_false_alarms():
    frame = pd.DataFrame({
        "delivery_date": pd.Timestamp("2026-01-01"),
        "period": [0, 0, 0, 1, 1, 1],
        "actual": ["down", "down", "up", "nothing", "down", "nothing"],
        "decision": ["down", "nothing", "down", "down", "up", "nothing"],
    })
    counts = metrics.decision_counts(frame)
    assert counts["correct"] == 2
    assert (counts["down_happened"], counts["down_said"], counts["down_right"]) == (3, 3, 1)
    assert (counts["down_missed"], counts["down_wrong_direction"], counts["down_false_alarms"]) == (1, 1, 2)
    assert (counts["up_happened"], counts["up_right"], counts["up_wrong_direction"], counts["up_false_alarms"]) == (1, 0, 1, 1)
    assert (counts["down_quarters_happened"], counts["down_quarters_said"], counts["down_quarters_one_unit_right"]) == (2, 2, 1)
    assert (counts["up_quarters_happened"], counts["up_quarters_missed"], counts["up_quarters_false_alarm"]) == (1, 1, 1)


def test_each_direction_uses_its_own_restriction_codes_and_model_settings():
    loaded = load_config(DEFAULT_CONFIG)
    down, up = loaded.for_direction("Bajar"), loaded.for_direction("Subir")
    assert {"SCB", "SCA", "RTD", "EST"} <= set(down.labels.restriction_types)
    assert {"COT", "SOT", "RSI"} <= set(up.labels.restriction_types)
    assert up.labels.direction == "Subir"
    assert down.model.n_estimators > up.model.n_estimators == loaded.model.n_estimators


def test_recency_weight_halves_every_half_life():
    dates = pd.Series(pd.to_datetime(["2026-07-01", "2026-04-02", "2026-01-02"]))
    weights = estimator._recency(dates, pd.Timestamp("2026-07-01"), 90)
    assert np.allclose(weights, [1.0, 0.5, 0.25])
    assert estimator._recency(dates, pd.Timestamp("2026-07-01"), 0).tolist() == [1.0, 1.0, 1.0]


def test_negative_sampling_keeps_positives_and_reweights_negatives():
    frame = pd.DataFrame({"target": [1.0] * 100 + [0.0] * 10000})
    kept, weights = estimator._sample(frame, 0.25, 0)
    assert (kept["target"] == 1).sum() == 100
    assert abs((kept["target"] == 0).sum() - 2500) < 200
    assert set(weights[kept["target"].to_numpy() == 0]) == {4.0}
    assert abs(weights.sum() - len(frame)) < 800


def test_frequent_units_counts_only_large_redispatch_before_the_holdout(config):
    days = list(pd.date_range("2026-01-01", periods=30, freq="D"))
    often = [(day, period, "OFTEN", 5.0) for day in days[:20] for period in range(3)]
    late = [(day, period, "LATE", 5.0) for day in days[-config.model.test_days:] for period in range(10)]
    small = [(day, 0, "SMALL", 1.0) for day in days]
    redispatch = pd.DataFrame(often + late + small, columns=["delivery_date", "period", "unit", "mwh"])
    assert pipeline.frequent_units(config, redispatch, days) == {"OFTEN"}


def test_technology_groups_registry_production_types():
    assert locations.technology_group("Solar fotovoltaica") == "solar_pv"
    assert locations.technology_group("Eólica terrestre") == "wind"
    assert locations.technology_group("Ciclo Combinado") == "combined_cycle"
    assert locations.technology_group("Consumo bombeo") == "pumped_hydro"
    assert locations.technology_group("Hidráulica no UGH") == "hydro"
    assert locations.technology_group("Batería electroquímica") == "other"
    assert locations.technology_group(None) == "unknown"
    assert set(locations.TECHNOLOGIES) >= {"solar_pv", "wind", "other", "unknown"}


def test_located_units_keeps_units_whose_plants_are_all_located_with_the_wanted_confidence(config, tmp_path):
    path = tmp_path / "locations.csv"
    pd.DataFrame({
        "programming_unit": ["SURE", "SURE", "MIXED", "MIXED", "NONE"],
        "confidence": ["high", "high", "high", "medium", None],
    }).to_csv(path, index=False)
    located = dataclasses.replace(config, paths=dataclasses.replace(config.paths, unit_locations=path))
    assert pipeline.located_units(located, {"SURE", "MIXED", "NONE", "UNKNOWN"}) == {"SURE"}
    every = dataclasses.replace(located, units=dataclasses.replace(config.units, location_confidence=""))
    assert pipeline.located_units(every, {"SURE", "MIXED"}) == {"SURE", "MIXED"}


def test_listed_units_keeps_only_the_units_in_the_list_when_it_is_on(config, tmp_path):
    path = tmp_path / "units.csv"
    pd.DataFrame({"unit": ["SAGU1", "PALOS2"]}).to_csv(path, index=False)
    listed = dataclasses.replace(config, paths=dataclasses.replace(config.paths, unit_list=path))
    assert pipeline.listed_units(listed, {"SAGU1", "EOLA"}) == {"SAGU1"}
    off = dataclasses.replace(listed, units=dataclasses.replace(config.units, use_list=False))
    assert pipeline.listed_units(off, {"SAGU1", "EOLA"}) == {"SAGU1", "EOLA"}


def test_train_takes_the_number_of_test_days_from_the_command_line():
    from congestion_es import cli

    args = cli.build_parser().parse_args(["train", "--test-days", "1"])
    assert args.test_days == 1 and args.func is cli.cmd_train
    assert cli.build_parser().parse_args(["train"]).test_days is None


def test_spain_schedule_joins_the_national_inputs_under_readable_names(config):
    inputs = _inputs(config, days=10)
    schedule = inputs.market[["delivery_date", "period"]].assign(pbf_combined_cycle=1500.0, pbf_solar_pv=6000.0)
    with_schedule = dataclasses.replace(inputs, schedule=schedule)
    dataset = pipeline.build(config, with_schedule, None)
    assert "spain_schedule_combined_cycle_mwh" in feature_columns(config)
    assert dataset["spain_schedule_combined_cycle_mwh"].eq(1500.0).all()
    assert dataset["spain_schedule_nuclear_mwh"].isna().all()
    assert set(feature_columns(config)) <= set(dataset.columns)


def test_backtest_trains_each_day_only_on_the_days_before_it(config, monkeypatch):
    synthetic = {direction: _inputs(config, direction, days=60) for direction in pipeline.DIRECTION_NAMES}
    monkeypatch.setattr(pipeline, "load_inputs", lambda settings, units: synthetic[settings.labels.direction])
    trained_until = []
    fit = estimator.fit
    monkeypatch.setattr(estimator, "fit", lambda settings, categorical, features, rows, end: trained_until.append((rows["delivery_date"].max(), end)) or fit(settings, categorical, features, rows, end))
    decisions = pipeline.backtest(config, synthetic["Bajar"].units, 3)
    labelled = sorted(synthetic["Bajar"].label_days)
    assert sorted(decisions["delivery_date"].unique()) == labelled[-3:]
    assert len(decisions) == 3 * 96 * len(UNITS)
    for (latest, end), day in zip(trained_until, labelled[-3:] * 2):
        assert latest == end == day - pd.Timedelta(days=1)
    assert set(decisions["decision"]) <= {"down", "up", "nothing"}


def test_result_tables_show_happened_spotted_correct_and_wrong_split():
    frame = pd.DataFrame({
        "delivery_date": pd.Timestamp("2026-07-17"), "period": [0, 1, 2, 3],
        "actual": ["up", "up", "nothing", "down"], "decision": ["up", "nothing", "up", "down"],
    })
    text = metrics.result_tables({"17 Jul 2026": frame}, "test")
    lines = text.splitlines()
    assert lines[0] == "DOWN (test)" and "UP (test)" in lines
    up_row = [line for line in lines[lines.index("UP (test)"):] if line.startswith("│ 17 Jul")][0]
    assert [int(cell) for cell in up_row.split("│")[2:-1]] == [2, 2, 1, 2, 1, 1]
