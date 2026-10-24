from __future__ import annotations

import json
import math
import time
import urllib.error
import urllib.parse
import urllib.request

import pandas as pd

from .timeseries import add_period

ARCHIVE_URL = "https://historical-forecast-api.open-meteo.com/v1/forecast"
LIVE_URL = "https://api.open-meteo.com/v1/forecast"
VARIABLES = ("wind_speed_100m", "shortwave_radiation", "cloud_cover")
TIMEZONE = "Europe/Madrid"
LIVE_DAYS = 7
KEY = ["delivery_date", "period", "point"]


def point_id(lat: float, lon: float, size: float) -> str:
    """Name the square of the given size containing a point by its south-west corner."""
    return f"{math.floor(lat / size) * size:.2f}_{math.floor(lon / size) * size:.2f}"


def spot_id(lat: float, lon: float, size: float) -> str:
    """Name the nearest weather spot on a grid of the given size."""
    return f"{round(lat / size) * size:.2f}_{round(lon / size) * size:.2f}"


def spots_for(places: pd.DataFrame, size: float) -> pd.DataFrame:
    """Return the weather spots, with their coordinates, nearest to the given unit places."""
    ids = pd.Series([spot_id(lat, lon, size) for lat, lon in zip(places["lat"], places["lon"])], dtype=str).drop_duplicates()
    corners = ids.str.split("_", expand=True).astype(float)
    return pd.DataFrame({"point": ids.to_numpy(), "lat": corners[0].to_numpy(), "lon": corners[1].to_numpy()})


def area_weather(places: pd.DataFrame, spots: pd.DataFrame, forecast: pd.DataFrame, area_size: float) -> tuple[pd.DataFrame, pd.DataFrame]:
    """Average the spot forecasts over each area and map every placed unit to its area's average."""
    areas = spots.assign(area=[point_id(lat, lon, area_size) for lat, lon in zip(spots["lat"], spots["lon"])])
    averaged = (
        forecast.merge(areas[["point", "area"]], on="point", how="inner")
        .groupby(["delivery_date", "period", "area"], as_index=False)[list(VARIABLES)].mean()
        .rename(columns={"area": "point"})
    )
    points = places[["unit"]].assign(point=[point_id(lat, lon, area_size) for lat, lon in zip(places["lat"], places["lon"])])
    return points.reset_index(drop=True), averaged


def request_weight(points: int, first: pd.Timestamp, last: pd.Timestamp) -> int:
    """Count the Open-Meteo calls a request uses: one per point and started two weeks of days."""
    return points * math.ceil((last - first).days / 14)


def _request(url: str, params: dict, opener=urllib.request.urlopen, sleep=time.sleep, attempts: int = 6) -> list[dict]:
    """Call Open-Meteo, retrying rate limits, server errors and empty or broken responses with growing waits."""
    full = url + "?" + urllib.parse.urlencode(params)
    for attempt in range(attempts):
        try:
            with opener(urllib.request.Request(full, headers={"User-Agent": "redispatch-es"}), timeout=300) as response:
                data = json.loads(response.read())
            return data if isinstance(data, list) else [data]
        except urllib.error.HTTPError as error:
            if error.code not in (429, 500, 502, 503, 504) or attempt == attempts - 1:
                raise
            sleep(120 * (attempt + 1))
        except (json.JSONDecodeError, urllib.error.URLError, TimeoutError, ConnectionError):
            if attempt == attempts - 1:
                raise
            sleep(30 * (attempt + 1))
    return []


def to_quarters(responses: list[dict], points: list[str]) -> pd.DataFrame:
    """Interpolate Open-Meteo UTC hourly series to local quarter-hours per weather point."""
    frames = []
    for point, response in zip(points, responses):
        hourly = response["hourly"]
        index = pd.to_datetime(hourly["time"]).tz_localize("UTC")
        values = pd.DataFrame({name: pd.to_numeric(pd.Series(hourly.get(name)), errors="coerce").to_numpy() for name in VARIABLES}, index=index)
        values = values.resample("15min").interpolate("linear")
        frame = values.reset_index(drop=True)
        frame = add_period(frame, pd.Series(values.index.tz_convert(TIMEZONE)))
        frame["point"] = point
        frames.append(frame)
    frame = pd.concat(frames, ignore_index=True)
    return frame.groupby(KEY, as_index=False)[list(VARIABLES)].mean()


def fetch(
    centres: pd.DataFrame,
    start: pd.Timestamp,
    end: pd.Timestamp,
    today: pd.Timestamp,
    per_request: int = 10,
    interval_seconds: float = 5.0,
    calls_per_hour: float = 4000.0,
    request=_request,
    sleep=time.sleep,
) -> pd.DataFrame:
    """Fetch forecasts for every weather point and local delivery day in [start, end), at quarter-hours, within the hourly call budget."""
    live_from = today - pd.Timedelta(days=LIVE_DAYS)
    spans = []
    if start < min(end, live_from):
        spans.append((ARCHIVE_URL, start, min(end, live_from)))
    if end > max(start, live_from):
        spans.append((LIVE_URL, max(start, live_from), end))

    frames = []
    for url, first, last in spans:
        for i in range(0, len(centres), per_request):
            chunk = centres.iloc[i:i + per_request]
            params = {
                "latitude": ",".join(f"{v:.2f}" for v in chunk["lat"]),
                "longitude": ",".join(f"{v:.2f}" for v in chunk["lon"]),
                "start_date": f"{first - pd.Timedelta(days=1):%Y-%m-%d}",
                "end_date": f"{last - pd.Timedelta(days=1):%Y-%m-%d}",
                "hourly": ",".join(VARIABLES),
                "timezone": "GMT",
            }
            part = to_quarters(request(url, params), chunk["point"].tolist())
            frames.append(part[(part["delivery_date"] >= first) & (part["delivery_date"] < last)])
            sleep(max(interval_seconds, request_weight(len(chunk), first, last) * 3600 / calls_per_hour))
    if not frames:
        return pd.DataFrame(columns=KEY + list(VARIABLES))
    return pd.concat(frames, ignore_index=True)
