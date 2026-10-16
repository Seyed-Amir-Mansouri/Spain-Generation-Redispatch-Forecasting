from __future__ import annotations

import json
import time
import urllib.error
import urllib.request
from datetime import date

import pandas as pd

BASE_URL = "https://api.esios.ree.es"
TIMEZONE = "Europe/Madrid"


class Client:
    def __init__(self, api_key: str, interval_seconds: float, opener=urllib.request.urlopen, sleep=time.sleep, clock=time.monotonic):
        self.api_key = api_key
        self.interval = interval_seconds
        self.opener = opener
        self.sleep = sleep
        self.clock = clock
        self._last = None

    def get(self, path: str) -> bytes:
        """Fetch a path relative to the API root and return the raw body."""
        if self._last is not None:
            wait = self.interval - (self.clock() - self._last)
            if wait > 0:
                self.sleep(wait)
        request = urllib.request.Request(
            BASE_URL + path,
            headers={
                "Accept": "application/json; application/vnd.esios-api-v1+json",
                "Content-Type": "application/json",
                "x-api-key": self.api_key,
            },
        )
        try:
            with self.opener(request, timeout=300) as response:
                return response.read()
        finally:
            self._last = self.clock()

    def indicator(self, indicator_id: int, start: date, end: date) -> pd.DataFrame:
        """Return an indicator's peninsular values between two dates as hourly local sums."""
        path = (
            f"/indicators/{indicator_id}"
            f"?start_date={start:%Y-%m-%d}T00:00:00&end_date={end:%Y-%m-%d}T23:59:59"
        )
        values = json.loads(self.get(path))["indicator"]["values"]
        return hourly_values(values)

    def archive(self, archive_id: int, day: date) -> bytes | None:
        """Download one day's file archive, or None when REE has no file for that day."""
        try:
            body = self.get(f"/archives/{archive_id}/download?date={day:%Y-%m-%d}T00:00:00")
        except urllib.error.HTTPError as error:
            if error.code == 404:
                return None
            raise
        return None if body[:2] != b"PK" else body

    def json_archive(self, archive_id: int) -> dict:
        """Download a structural JSON archive such as the unit registries."""
        return json.loads(self.get(f"/archives/{archive_id}/download_json?locale=es").decode("utf-8-sig"))


def hourly_values(values: list[dict]) -> pd.DataFrame:
    """Sum indicator values per local delivery date and hour, keeping the peninsular system only."""
    frame = pd.DataFrame(values)
    if frame.empty:
        return pd.DataFrame({
            "delivery_date": pd.Series(dtype="datetime64[ns]"),
            "hour": pd.Series(dtype="int64"),
            "value": pd.Series(dtype="float64"),
        })
    if "geo_name" in frame and (frame["geo_name"] == "Península").any():
        frame = frame[frame["geo_name"] == "Península"]
    stamps = pd.to_datetime(frame["datetime"], utc=True).dt.tz_convert(TIMEZONE)
    frame = frame.assign(delivery_date=stamps.dt.normalize().dt.tz_localize(None), hour=stamps.dt.hour)
    return frame.groupby(["delivery_date", "hour"], as_index=False)["value"].sum()
