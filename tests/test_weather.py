import pandas as pd

from congestion_es import weather
from congestion_es.features import add_weather


def test_units_map_to_their_weather_square_or_the_nearest_available_one():
    places = pd.DataFrame({"unit": ["A", "B", "C"], "lat": [38.7, 38.2, 43.9], "lon": [-6.3, -6.9, -0.2]})
    points, centres = weather.points_for(places, 1.0)
    assert points.set_index("unit")["point"].to_dict() == {"A": "38.00_-7.00", "B": "38.00_-7.00", "C": "43.00_-1.00"}
    assert centres.set_index("point").loc["38.00_-7.00", ["lat", "lon"]].tolist() == [38.5, -6.5]
    nearest, _ = weather.points_for(places, 1.0, {"38.00_-7.00", "41.00_-1.00"})
    assert nearest.set_index("unit").loc["C", "point"] == "41.00_-1.00"


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


def test_add_weather_gives_each_unit_its_point_forecast_with_readable_names():
    frame = pd.DataFrame({"delivery_date": pd.to_datetime(["2026-07-01"] * 2), "period": [48, 48], "unit": ["a", "b"]})
    points = pd.DataFrame({"unit": ["a", "b"], "point": ["p", "q"]})
    forecast = pd.DataFrame({
        "delivery_date": pd.to_datetime(["2026-07-01"] * 2), "period": [48, 48], "point": ["p", "q"],
        "wind_speed_100m": [10.0, 20.0], "shortwave_radiation": [800.0, 400.0], "temperature_2m": [30.0, 25.0],
    })
    result = add_weather(frame, forecast, points).set_index("unit")
    assert result.loc["b", "wind_speed_kmh"] == 20.0
    assert result.loc["a", "solar_radiation_wm2"] == 800.0
