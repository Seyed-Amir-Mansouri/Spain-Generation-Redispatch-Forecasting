from datetime import datetime, timezone

import numpy as np
import pandas as pd

from congestion_es import estimator, ledger, pipeline, validate, weather
from congestion_es.areas import CAPACITY_COLUMNS
from congestion_es.features import feature_columns
from congestion_es.timeseries import hours_to_quarters

AREAS = ["37.50_-6.00", "38.50_-6.50", "41.00_-1.00"]


def _inputs(config, days=200, seed=5, extra_labels=None):
    """Synthetic inputs where congestion in the south-west follows the national contingency total."""
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

    capacity = pd.DataFrame({"area": AREAS, "cell_lat": [37.75, 38.75, 41.25], "cell_lon": [-5.75, -6.25, -0.75]})
    for column in CAPACITY_COLUMNS:
        capacity[column] = [400.0, 300.0, 200.0]
    capacity["mw_total"] = capacity[CAPACITY_COLUMNS].sum(axis=1)

    rows = []
    for (day, period), value in zip(national[["delivery_date", "period"]].itertuples(index=False), pressure):
        if value > 500:
            rows.append((day, period, AREAS[0], value / 10))
        if value > 900:
            rows.append((day, period, AREAS[1], value / 20))
    attributed = pd.DataFrame(rows + (extra_labels or []), columns=["delivery_date", "period", "area", "mwh"])
    points, _ = weather.points_for(capacity, config.weather.grid_degrees)
    forecast = national.merge(points[["point"]].drop_duplicates(), how="cross")
    m = len(forecast)
    forecast = forecast.assign(
        wind_speed_100m=rng.gamma(2, 10, m), shortwave_radiation=rng.uniform(0, 900, m), temperature_2m=rng.normal(18, 6, m)
    )
    return pipeline.Inputs(
        market=market, constraints=constraints, capacity=capacity, weather=forecast, points=points, attributed=attributed,
        label_days=set(dates[: days - 20]), area_codes={a: i for i, a in enumerate(sorted(AREAS))}, coverage=0.7,
    )


def test_target_is_unknown_for_missing_days_and_after_label_end(config):
    inputs = _inputs(config, days=40)
    dataset = pipeline.build(config, inputs, pd.Timestamp("2025-06-25"))
    dates = dataset["delivery_date"]
    assert dataset.loc[dates <= pd.Timestamp("2025-06-20"), "target"].notna().all()
    assert dataset.loc[dates.between(pd.Timestamp("2025-06-21"), pd.Timestamp("2025-06-25")), "target"].isna().all()
    assert dataset.loc[dates > pd.Timestamp("2025-06-25"), "target"].isna().all()


def test_recent_congestion_does_not_leak_into_history_features(config):
    base = _inputs(config, days=120)
    day = pd.Timestamp("2025-09-20")
    extra = [(day - pd.Timedelta(days=5), 48, AREAS[2], 999.0)]
    changed = _inputs(config, days=120, extra_labels=extra)
    columns = ["area_congested_hist", "area_congested_hist_long", "area_period_congested_hist"]
    a = pipeline.build(config, base, None)
    b = pipeline.build(config, changed, None)
    rows_a = a[(a["delivery_date"] == day) & (a["area"] == AREAS[2])][columns].fillna(-1)
    rows_b = b[(b["delivery_date"] == day) & (b["area"] == AREAS[2])][columns].fillna(-1)
    assert np.allclose(rows_a.to_numpy(), rows_b.to_numpy())


def test_train_and_predict_rank_the_congested_cell_first(config):
    inputs = _inputs(config)
    dataset = pipeline.build(config, inputs, pd.Timestamp("2025-12-17"))
    bundle, report = pipeline.train(config, inputs, dataset)
    assert report["holdout"]["roc_auc"] > 0.8
    assert bundle["features"] == feature_columns(config)

    day = pd.Timestamp("2025-12-10")
    predictions = pipeline.predict_day(bundle, pipeline.build(config, inputs, None), day, datetime(2025, 12, 9, 12, tzinfo=timezone.utc))
    assert list(predictions.columns) == ledger.PREDICTION_COLUMNS
    assert len(predictions) == 96 * len(AREAS)
    means = predictions.groupby("area")["risk_probability"].mean()
    assert means.idxmax() == AREAS[0]
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
    merged = dataset.merge(inputs.attributed, on=["delivery_date", "period", "area"], how="left")
    labelled = merged["target"].notna()
    assert np.allclose(merged.loc[labelled, "target_mwh"], merged.loc[labelled, "mwh"].fillna(0.0))
    assert merged.loc[~labelled, "target_mwh"].isna().all()


def test_expected_mwh_follows_the_congested_cell_and_is_saved_per_direction(config, tmp_path):
    inputs = _inputs(config)
    dataset = pipeline.build(config, inputs, pd.Timestamp("2025-12-17"))
    bundle, report = pipeline.train(config.for_direction("Subir"), inputs, dataset)
    assert report["holdout_energy"]["corr_cell_day"] > 0.5
    assert bundle["direction"] == "Subir"

    path = estimator.save(bundle, tmp_path)
    assert path.name == "redispatch_model_subir.joblib"
    loaded = estimator.load(tmp_path, "Subir")
    predictions = pipeline.predict_day(loaded, pipeline.build(config, inputs, None), pd.Timestamp("2025-12-10"))
    assert (predictions["direction"] == "Subir").all()
    assert (predictions["expected_mwh"] >= 0).all()
    totals = predictions.groupby("area")["expected_mwh"].sum()
    assert totals.idxmax() == AREAS[0]


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
    assert result["overall_n"] == len(days) * 96 * len(AREAS)
    assert result["overall_corr_cell_day"] > 0.5


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
