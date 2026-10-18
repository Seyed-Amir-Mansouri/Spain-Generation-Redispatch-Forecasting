from datetime import datetime, timezone

import numpy as np
import pandas as pd

from congestion_es import estimator, ledger, pipeline, validate, weather
from congestion_es.features import feature_columns
from congestion_es.timeseries import hours_to_quarters

UNITS = ["EOLA", "FVB", "CCGC"]


def _inputs(config, days=200, seed=5, extra_labels=None):
    """Synthetic inputs where two units' redispatch follows the national contingency total."""
    rng = np.random.default_rng(seed)
    dates = pd.date_range("2025-06-01", periods=days, freq="D")
    national = pd.MultiIndex.from_product([dates, range(96)], names=["delivery_date", "period"]).to_frame(index=False)
    n = len(national)
    market = national.assign(
        da_price=rng.normal(60, 20, n), ida1_price=rng.normal(60, 20, n), ida2_price=rng.normal(60, 20, n),
        ida3_price=rng.normal(60, 20, n), load_forecast_mw=rng.normal(28000, 3000, n),
        wind_forecast_mw=rng.gamma(2, 3000, n), solar_forecast_mw=rng.gamma(2, 4000, n),
    )
    pressure = rng.gamma(1.5, 300, n)
    constraints = national.copy()
    for name in config.constraint_indicators:
        constraints[name] = rng.gamma(1.5, 100, n)
    constraints["down_overload_contingency"] = pressure

    units = pd.DataFrame({"unit": UNITS, "unit_id": [0, 1, 2], "unit_redispatch_rate": [0.3, 0.1, 0.0], "unit_mean_mwh": [60.0, 50.0, 0.0]})
    places = pd.DataFrame({"unit": UNITS, "lat": [37.7, 38.7, 41.2], "lon": [-5.7, -6.2, -0.7]})

    rows = []
    for (day, period), value in zip(national[["delivery_date", "period"]].itertuples(index=False), pressure):
        if value > 500:
            rows.append((day, period, UNITS[0], value / 10))
        if value > 900:
            rows.append((day, period, UNITS[1], value / 20))
    redispatch = pd.DataFrame(rows + (extra_labels or []), columns=["delivery_date", "period", "unit", "mwh"])
    points, _ = weather.points_for(places, config.weather.grid_degrees)
    forecast = national.merge(points[["point"]].drop_duplicates(), how="cross")
    m = len(forecast)
    forecast = forecast.assign(
        wind_speed_100m=rng.gamma(2, 10, m), shortwave_radiation=rng.uniform(0, 900, m), temperature_2m=rng.normal(18, 6, m)
    )
    return pipeline.Inputs(
        market=market, constraints=constraints, units=units, weather=forecast, points=points, redispatch=redispatch,
        label_days=set(dates[: days - 20]), coverage=0.7,
    )


def test_target_is_unknown_for_missing_days_and_after_label_end(config):
    inputs = _inputs(config, days=40)
    dataset = pipeline.build(config, inputs, pd.Timestamp("2025-06-25"))
    dates = dataset["delivery_date"]
    assert dataset.loc[dates <= pd.Timestamp("2025-06-20"), "target"].notna().all()
    assert dataset.loc[dates.between(pd.Timestamp("2025-06-21"), pd.Timestamp("2025-06-25")), "target"].isna().all()
    assert dataset.loc[dates > pd.Timestamp("2025-06-25"), "target"].isna().all()


def test_train_and_predict_rank_the_redispatched_unit_first(config):
    inputs = _inputs(config)
    dataset = pipeline.build(config, inputs, pd.Timestamp("2025-12-17"))
    bundle, report = pipeline.train(config, inputs, dataset)
    assert report["holdout"]["roc_auc"] > 0.8
    assert bundle["features"] == feature_columns(config)

    day = pd.Timestamp("2025-12-10")
    predictions = pipeline.predict_day(bundle, pipeline.build(config, inputs, None), day, datetime(2025, 12, 9, 12, tzinfo=timezone.utc))
    assert list(predictions.columns) == ledger.PREDICTION_COLUMNS
    assert len(predictions) == 96 * len(UNITS)
    means = predictions.groupby("unit")["risk_probability"].mean()
    assert means.idxmax() == UNITS[0]
    assert estimator.predict(bundle, dataset.head(5)).shape == (5,)


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
    bundle, report = pipeline.train(config, inputs, sampled)
    assert report["holdout"]["n"] == late.sum()


def test_hours_to_quarters_repeats_rows_and_splits_energy():
    hourly = pd.DataFrame({"delivery_date": pd.Timestamp("2025-06-01"), "hour": [0, 23], "price": [50.0, 70.0], "mwh": [8.0, 4.0]})
    quarters = hours_to_quarters(hourly, ("mwh",))
    assert quarters["period"].tolist() == [0, 1, 2, 3, 92, 93, 94, 95]
    assert quarters["price"].tolist() == [50.0] * 4 + [70.0] * 4
    assert quarters["mwh"].tolist() == [2.0] * 4 + [1.0] * 4


def test_target_mwh_is_the_attributed_energy_on_labelled_rows(config):
    inputs = _inputs(config, days=40)
    dataset = pipeline.build(config, inputs, pd.Timestamp("2025-06-25"))
    merged = dataset.merge(inputs.redispatch, on=["delivery_date", "period", "unit"], how="left")
    labelled = merged["target"].notna()
    assert np.allclose(merged.loc[labelled, "target_mwh"], merged.loc[labelled, "mwh"].fillna(0.0))
    assert merged.loc[~labelled, "target_mwh"].isna().all()


def test_expected_mwh_follows_the_redispatched_unit_and_is_saved_per_direction(config, tmp_path):
    inputs = _inputs(config)
    dataset = pipeline.build(config, inputs, pd.Timestamp("2025-12-17"))
    bundle, report = pipeline.train(config.for_direction("Subir"), inputs, dataset)
    assert report["holdout_energy"]["corr_unit_day"] > 0.5
    assert bundle["direction"] == "Subir"

    path = estimator.save(bundle, tmp_path)
    assert path.name == "redispatch_model_subir.joblib"
    loaded = estimator.load(tmp_path, "Subir")
    predictions = pipeline.predict_day(loaded, pipeline.build(config, inputs, None), pd.Timestamp("2025-12-10"))
    assert (predictions["direction"] == "Subir").all()
    assert (predictions["expected_mwh"] >= 0).all()
    totals = predictions.groupby("unit")["expected_mwh"].sum()
    assert totals.idxmax() == UNITS[0]


def test_validation_scores_only_its_direction_with_energy_measures(config):
    inputs = _inputs(config)
    dataset = pipeline.build(config, inputs, pd.Timestamp("2025-12-17"))
    bundle, _ = pipeline.train(config, inputs, dataset)
    features = pipeline.build(config, inputs, None)
    issued = datetime(2025, 11, 1, tzinfo=timezone.utc)
    days = pd.date_range("2025-11-01", "2025-11-10")
    predictions = pd.concat([pipeline.predict_day(bundle, features, day, issued) for day in days], ignore_index=True)
    upward = predictions.assign(direction="Subir", expected_mwh=0.0)
    ledger_frame = pd.concat([predictions, upward], ignore_index=True)
    ledger_frame["issued_at"] = pd.to_datetime(ledger_frame["issued_at"], utc=True)
    result = validate.evaluate(config.for_direction("Bajar"), ledger_frame, inputs, pd.Timestamp("2025-11-10"), pd.Timestamp("2026-02-10"))
    assert result["direction"] == "Bajar"
    assert result["overall_n"] == len(days) * 96 * len(UNITS)
    assert result["overall_corr_unit_day"] > 0.5


def test_each_direction_uses_its_own_restriction_codes(config):
    down, up = config.for_direction("Bajar"), config.for_direction("Subir")
    assert {"SCB", "SCA", "RTD", "EST"} <= set(down.labels.restriction_types)
    assert {"COT", "SOT", "RSI"} <= set(up.labels.restriction_types)
    assert up.labels.direction == "Subir"


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


def test_select_units_keeps_frequent_units_from_before_the_holdout(config):
    days = list(pd.date_range("2026-01-01", periods=30, freq="D"))
    often = [(day, period, "OFTEN", 5.0) for day in days[:20] for period in range(3)]
    late = [(day, period, "LATE", 5.0) for day in days[-config.model.test_days:] for period in range(10)]
    small = [(day, 0, "SMALL", 1.0) for day in days]
    redispatch = pd.DataFrame(often + late + small, columns=["delivery_date", "period", "unit", "mwh"])
    units = pipeline.select_units(config, redispatch, days)
    assert units["unit"].tolist() == ["OFTEN"]
    assert units.loc[0, "unit_redispatch_rate"] == np.float32(60 / (96 * 20))
    assert units.loc[0, "unit_mean_mwh"] == 5.0
