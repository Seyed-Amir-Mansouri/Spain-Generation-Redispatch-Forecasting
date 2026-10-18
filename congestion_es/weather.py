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
VARIABLES = ("wind_speed_100m", "shortwave_radiation", "temperature_2m")
TIMEZONE = "Europe/Madrid"
LIVE_DAYS = 7
KEY = ["delivery_date", "period", "point"]


def point_id(lat: float, lon: float, size: float) -> str:
    """Name the weather grid square containing a point by its south-west corner."""
    return f"{math.floor(lat / size) * size:.2f}_{math.floor(lon / size) * size:.2f}"


def points_for(places: pd.DataFrame, size: float, available=None) -> tuple[pd.DataFrame, pd.DataFrame]:
    """Map each unit's location to its weather square, or the nearest available one, and return the squares' centres."""
    frame = places[["unit", "lat", "lon"]].dropna().copy()
    frame["point"] = [point_id(lat, lon, size) for lat, lon in zip(frame["lat"], frame["lon"])]
    if available is not None and len(available):
        known = pd.Series(sorted(available))
        corners = known.str.split("_", expand=True).astype(float).to_numpy() + size / 2
        missing = ~frame["point"].isin(set(known))
        for index in frame.index[missing]:
            distance = (corners[:, 0] - frame.at[index, "lat"]) ** 2 + (corners[:, 1] - frame.at[index, "lon"]) ** 2
            frame.at[index, "point"] = known.iloc[int(distance.argmin())]
    centres = frame.drop_duplicates("point")[["point"]].copy()
    corners = centres["point"].str.split("_", expand=True).astype(float)
    centres["lat"] = corners[0] + size / 2
    centres["lon"] = corners[1] + size / 2
    return frame[["unit", "point"]].reset_index(drop=True), centres.reset_index(drop=True)


def _request(url: str, params: dict, opener=urllib.request.urlopen, sleep=time.sleep, attempts: int = 6) -> list[dict]:
    """Call Open-Meteo, retrying rate limits and empty or broken responses with growing waits."""
    full = url + "?" + urllib.parse.urlencode(params)
    for attempt in range(attempts):
        try:
            with opener(urllib.request.Request(full, headers={"User-Agent": "grid-congestion"}), timeout=300) as response:
                data = json.loads(response.read())
            return data if isinstance(data, list) else [data]
        except urllib.error.HTTPError as error:
            if error.code != 429 or attempt == attempts - 1:
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
    request=_request,
    sleep=time.sleep,
) -> pd.DataFrame:
    """Fetch forecasts for every weather point and local delivery day in [start, end), at quarter-hours."""
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
            sleep(interval_seconds)
    if not frames:
        return pd.DataFrame(columns=KEY + list(VARIABLES))
    return pd.concat(frames, ignore_index=True)
