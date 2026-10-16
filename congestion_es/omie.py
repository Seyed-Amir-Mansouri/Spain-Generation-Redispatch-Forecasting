from __future__ import annotations

import time
import urllib.error
import urllib.request
from datetime import date

import numpy as np
import pandas as pd

from .i90 import period_hours

URL = "https://www.omie.es/es/file-download?parents=marginalpibc&filename=marginalpibc_{day:%Y%m%d}{session:02d}.1"
SESSIONS = (1, 2, 3)
SPAIN_PRICE_FIELD = 5
TIMEZONE = "Europe/Madrid"


def _periods_in_day(day: date, step_minutes: int) -> int:
    """Count the periods in a local delivery day, which differs on clock-change days."""
    start = pd.Timestamp(day).tz_localize(TIMEZONE)
    end = (pd.Timestamp(day) + pd.Timedelta(days=1)).tz_localize(TIMEZONE)
    return int((end - start) / pd.Timedelta(minutes=step_minutes))


def parse(text: str, day: date) -> pd.DataFrame:
    """Turn one OMIE marginal price file into hourly mean Spanish prices."""
    rows = [line.strip().rstrip(";").split(";") for line in text.splitlines()]
    rows = [row for row in rows if len(row) > SPAIN_PRICE_FIELD and row[3].strip().isdigit()]
    if not rows:
        return pd.DataFrame(columns=["hour", "price"])
    periods = np.array([int(row[3]) for row in rows])
    prices = np.array([float(row[SPAIN_PRICE_FIELD]) for row in rows])
    step = 15 if periods.max() > 25 else 60
    hours = period_hours(day, _periods_in_day(day, step))
    valid = periods <= len(hours)
    frame = pd.DataFrame({"hour": hours[periods[valid] - 1], "price": prices[valid]})
    return frame.groupby("hour", as_index=False)["price"].mean()


def _download(day: date, session: int, opener=urllib.request.urlopen, sleep=time.sleep, attempts: int = 4) -> str | None:
    """Fetch one session file, retrying temporary failures; None only when OMIE has no such file."""
    request = urllib.request.Request(URL.format(day=day, session=session), headers={"User-Agent": "grid-congestion"})
    for attempt in range(attempts):
        try:
            with opener(request, timeout=60) as response:
                text = response.read().decode("latin-1")
            if text.startswith("MARGINALPIBC"):
                return text
        except urllib.error.HTTPError as error:
            if error.code == 404:
                return None
        except (urllib.error.URLError, TimeoutError, ConnectionError):
            pass
        if attempt < attempts - 1:
            sleep(5 * (attempt + 1))
    return None


def fetch_intraday(days, interval_seconds: float = 1.0, download=_download, sleep=time.sleep) -> pd.DataFrame:
    """Return hourly Spanish IDA1, IDA2 and IDA3 prices for each delivery day."""
    frames = []
    for day in days:
        for session in SESSIONS:
            text = download(day, session)
            sleep(interval_seconds)
            if text is None:
                continue
            prices = parse(text, day).rename(columns={"price": f"ida{session}_price"})
            frames.append(prices.assign(delivery_date=pd.Timestamp(day)))
    columns = ["delivery_date", "hour"] + [f"ida{s}_price" for s in SESSIONS]
    if not frames:
        return pd.DataFrame(columns=columns)
    frame = pd.concat(frames, ignore_index=True)
    frame = frame.groupby(["delivery_date", "hour"], as_index=False).first()
    return frame.reindex(columns=columns)
