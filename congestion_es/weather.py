from __future__ import annotations

import json
import math
import time
import urllib.error
import urllib.parse
import urllib.request

import numpy as np
import pandas as pd

ARCHIVE_URL = "https://historical-forecast-api.open-meteo.com/v1/forecast"
LIVE_URL = "https://api.open-meteo.com/v1/forecast"
VARIABLES = ("wind_speed_100m", "shortwave_radiation", "temperature_2m")
TIMEZONE = "Europe/Madrid"
LIVE_DAYS = 7
KEY = ["delivery_date", "hour", "point"]


def point_id(lat: float, lon: float, size: float) -> str:
    """Name the weather grid square containing a point by its south-west corner."""
    return f"{math.floor(lat / size) * size:.2f}_{math.floor(lon / size) * size:.2f}"


def points_for(capacity: pd.DataFrame, size: float) -> pd.DataFrame:
    """Map each congestion cell to a weather point and return the points with their centres."""
    frame = capacity[["area", "cell_lat", "cell_lon"]].copy()
    frame["point"] = [point_id(lat, lon, size) for lat, lon in zip(frame["cell_lat"], frame["cell_lon"])]
    centres = frame.drop_duplicates("point")[["point"]].copy()
    corners = centres["point"].str.split("_", expand=True).astype(float)
    centres["lat"] = corners[0] + size / 2
    centres["lon"] = corners[1] + size / 2
    return frame[["area", "point"]], centres.reset_index(drop=True)


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


def to_hourly(responses: list[dict], points: list[str]) -> pd.DataFrame:
    """Convert Open-Meteo UTC hourly series into local delivery hours per weather point."""
    frames = []
    for point, response in zip(points, responses):
        hourly = response["hourly"]
        stamps = pd.to_datetime(hourly["time"]).tz_localize("UTC").tz_convert(TIMEZONE)
        frame = pd.DataFrame({name: hourly.get(name) for name in VARIABLES})
        frame["delivery_date"] = stamps.normalize().tz_localize(None)
        frame["hour"] = stamps.hour
        frame["point"] = point
        frames.append(frame)
    frame = pd.concat(frames, ignore_index=True)
    frame[list(VARIABLES)] = frame[list(VARIABLES)].apply(pd.to_numeric, errors="coerce")
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
    """Fetch hourly forecasts for every weather point and local delivery day in [start, end)."""
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
            part = to_hourly(request(url, params), chunk["point"].tolist())
            frames.append(part[(part["delivery_date"] >= first) & (part["delivery_date"] < last)])
            sleep(interval_seconds)
    if not frames:
        return pd.DataFrame(columns=KEY + list(VARIABLES))
    return pd.concat(frames, ignore_index=True)


def wind_output(speed_kmh: pd.Series) -> np.ndarray:
    """Return a generic turbine's output as a share of capacity for a 100 m wind speed."""
    speed = np.asarray(speed_kmh, dtype=float) / 3.6
    ramp = np.clip((speed - 3.0) / (12.0 - 3.0), 0.0, 1.0) ** 3
    return np.where(speed > 25.0, 0.0, ramp)


def solar_output(radiation: pd.Series) -> np.ndarray:
    """Return a solar plant's output as a share of capacity for a global horizontal irradiance."""
    return np.clip(np.asarray(radiation, dtype=float) / 1000.0, 0.0, 1.0)
