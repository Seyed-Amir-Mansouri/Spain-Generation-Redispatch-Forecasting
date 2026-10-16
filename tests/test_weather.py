import numpy as np
import pandas as pd

from congestion_es import weather
from congestion_es.features import add_weather


def test_cells_map_into_coarser_weather_squares():
    capacity = pd.DataFrame({"area": ["38.50_-6.50", "38.00_-6.50", "41.00_-1.00"], "cell_lat": [38.75, 38.25, 41.25], "cell_lon": [-6.25, -6.25, -0.75]})
    points, centres = weather.points_for(capacity, 1.0)
    assert points.set_index("area")["point"].to_dict() == {"38.50_-6.50": "38.00_-7.00", "38.00_-6.50": "38.00_-7.00", "41.00_-1.00": "41.00_-1.00"}
    assert centres.set_index("point").loc["38.00_-7.00", ["lat", "lon"]].tolist() == [38.5, -6.5]


def test_to_quarters_interpolates_utc_hours_to_local_quarters_on_a_clock_change_day():
    times = pd.date_range("2025-10-25 22:00", "2025-10-27 01:00", freq="h", inclusive="left").strftime("%Y-%m-%dT%H:%M").tolist()
    response = {"hourly": {"time": times, "wind_speed_100m": list(range(len(times))), "shortwave_radiation": [0] * len(times), "temperature_2m": [10] * len(times)}}
    frame = weather.to_quarters([response], ["p"])
    day = frame[frame["delivery_date"] == pd.Timestamp("2025-10-26")].set_index("period")["wind_speed_100m"]
    assert len(day) == 96
    assert day[8] == 2.5
    assert day[9] == 2.75


def test_fetch_uses_the_archive_for_history_and_the_live_service_for_recent_days():
    centres = pd.DataFrame({"point": ["p"], "lat": [38.5], "lon": [-6.5]})
    calls = []

    def request(url, params):
        calls.append((url, params["start_date"], params["end_date"]))
        times = pd.date_range(params["start_date"], pd.Timestamp(params["end_date"]) + pd.Timedelta(days=1), freq="h", inclusive="left")
        values = [1.0] * len(times)
        return [{"hourly": {"time": times.strftime("%Y-%m-%dT%H:%M").tolist(), "wind_speed_100m": values, "shortwave_radiation": values, "temperature_2m": values}}]

    frame = weather.fetch(centres, pd.Timestamp("2026-10-01"), pd.Timestamp("2026-10-16"), pd.Timestamp("2026-10-16"), request=request, sleep=lambda s: None)
    assert [c[0] for c in calls] == [weather.ARCHIVE_URL, weather.LIVE_URL]
    assert frame["delivery_date"].min() == pd.Timestamp("2026-10-01")
    assert frame["delivery_date"].max() == pd.Timestamp("2026-10-15")
    assert len(frame) == 15 * 96


def test_output_proxies_follow_simple_power_curves():
    assert np.allclose(weather.wind_output(pd.Series([0.0, 43.2, 54.0, 100.0])), [0.0, 1.0, 1.0, 0.0])
    assert np.allclose(weather.solar_output(pd.Series([-5.0, 500.0, 1200.0])), [0.0, 0.5, 1.0])


def test_add_weather_computes_expected_output_and_national_share():
    frame = pd.DataFrame({
        "delivery_date": pd.to_datetime(["2026-07-01"] * 2), "period": [48, 48], "area": ["a", "b"],
        "mw_wind": [0.0, 0.0], "mw_solar": [100.0, 300.0],
    })
    points = pd.DataFrame({"area": ["a", "b"], "point": ["p", "p"]})
    forecast = pd.DataFrame({"delivery_date": pd.to_datetime(["2026-07-01"]), "period": [48], "point": ["p"], "wind_speed_100m": [10.0], "shortwave_radiation": [800.0], "temperature_2m": [30.0]})
    result = add_weather(frame, forecast, points).set_index("area")
    assert result.loc["b", "expected_solar_mw"] == 240.0
    assert result.loc["a", "expected_renewable_share"] == 0.25
