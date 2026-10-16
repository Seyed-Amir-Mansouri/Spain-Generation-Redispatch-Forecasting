from datetime import datetime, timezone

import numpy as np
import pandas as pd

from congestion_es import estimator, ledger, pipeline, weather
from congestion_es.areas import CAPACITY_COLUMNS
from congestion_es.features import feature_columns

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


def test_negative_sampling_keeps_positives_and_reweights_negatives():
    frame = pd.DataFrame({"target": [1.0] * 100 + [0.0] * 10000})
    kept, weights = estimator._sample(frame, 0.25, 0)
    assert (kept["target"] == 1).sum() == 100
    assert abs((kept["target"] == 0).sum() - 2500) < 200
    assert set(weights[kept["target"].to_numpy() == 0]) == {4.0}
    assert abs(weights.sum() - len(frame)) < 800
