from __future__ import annotations

import re
import time
from pathlib import Path

import pandas as pd

TIMEZONE = "Europe/Madrid"
KEY = ["delivery_date", "period", "unit"]
QUARTER_COLUMNS = ["unit_gen_2d_mw", "plant_gen_2d_mw"]
DAY_COLUMNS = [
    "unit_gen_2d_day_mw", "unit_gen_1d_morning_mw", "unit_gen_1d_noon_mw",
    "unit_share_2d", "unit_rank_2d", "plant_gen_1d_morning_mw", "sisters_gen_1d_morning_mw",
]
COLUMNS = QUARTER_COLUMNS + DAY_COLUMNS


def build_client(api_key: str):
    """Construct an ENTSO-E raw client, which returns the XML documents."""
    from entsoe import EntsoeRawClient

    return EntsoeRawClient(api_key=api_key)


def _series(xml: str) -> list[str]:
    return re.findall(r"<TimeSeries>(.*?)</TimeSeries>", xml, re.S)


def plant_codes(xml: str) -> pd.DataFrame:
    """List each plant code with its programming unit's code and name, from a document that still names both."""
    rows = []
    for series in _series(xml):
        unit = re.search(r"<registeredResource.mRID[^>]*>([^<]+)<", series)
        if unit:
            for plant, name in re.findall(r"<PowerSystemResources>\s*<mRID[^>]*>([^<]+)</mRID>\s*<name>([^<]*)</name>", series):
                rows.append((plant, unit.group(1), name))
    return pd.DataFrame(rows, columns=["plant_eic", "eic", "name"]).drop_duplicates("plant_eic")


def parse(xml: str, plant_units: dict[str, str]) -> pd.DataFrame:
    """Read an actual-generation-per-unit document into MW per local quarter-hour and programming-unit code.

    From late December 2025 ENTSO-E sends only the plant code, so plant_units maps plant codes to programming-unit codes.
    """
    rows = []
    for series in _series(xml):
        plant = re.search(r"<PowerSystemResources>\s*<mRID[^>]*>([^<]+)<", series).group(1)
        unit = re.search(r"<registeredResource.mRID[^>]*>([^<]+)<", series)
        eic = unit.group(1) if unit else plant_units.get(plant)
        if eic is None:
            continue
        for period in re.findall(r"<Period>(.*?)</Period>", series, re.S):
            start = pd.Timestamp(re.search(r"<start>([^<]+)</start>", period).group(1))
            end = pd.Timestamp(re.search(r"<end>([^<]+)</end>", period).group(1))
            step = pd.Timedelta(minutes=int(re.search(r"<resolution>PT(\d+)M</resolution>", period).group(1)))
            points = {int(p): float(q) for p, q in re.findall(r"<position>(\d+)</position>\s*<quantity>([^<]+)</quantity>", period)}
            value = None
            # curve type A03: a position is left out when its value repeats the previous one
            for position in range(1, int((end - start) / step) + 1):
                value = points.get(position, value)
                if value is not None:
                    rows.append((start + (position - 1) * step, eic, plant, value))
    frame = pd.DataFrame(rows, columns=["stamp", "eic", "plant", "mw"])
    stamps = pd.to_datetime(frame["stamp"], utc=True).dt.tz_convert(TIMEZONE)
    frame["delivery_date"] = stamps.dt.normalize().dt.tz_localize(None)
    frame["period"] = stamps.dt.hour * 4 + stamps.dt.minute // 15
    # a programming unit can report several plants: average each plant over the quarter, then add the plants up
    frame = frame.groupby(["delivery_date", "period", "eic", "plant"], as_index=False)["mw"].mean()
    return frame.groupby(["delivery_date", "period", "eic"], as_index=False)["mw"].sum()


def fetch(client, days, folder: Path, plant_units: dict[str, str], interval_seconds: float, sleep=time.sleep) -> dict:
    """Save one CSV of per-unit actual generation per local day, skipping days already saved."""
    from entsoe.exceptions import NoMatchingDataError

    folder.mkdir(parents=True, exist_ok=True)
    stats = {"saved": 0, "cached": 0, "empty": 0}
    for day in days:
        path = folder / f"{day:%Y-%m-%d}.csv"
        if path.exists():
            stats["cached"] += 1
            continue
        start, end = day.tz_localize(TIMEZONE), (day + pd.Timedelta(days=1)).tz_localize(TIMEZONE)
        # the query takes at most 24 hours, so the 25-hour day of the October clock change goes in two halves
        spans = [(start, end)] if end - start <= pd.Timedelta(hours=24) else [(start, start + pd.Timedelta(hours=12)), (start + pd.Timedelta(hours=12), end)]
        parts = []
        for first, last in spans:
            try:
                parts.append(parse(client.query_generation_per_plant("ES", start=first, end=last), plant_units))
            except NoMatchingDataError:
                pass
            sleep(interval_seconds)
        frame = pd.concat(parts) if parts else pd.DataFrame()
        if frame.empty:
            stats["empty"] += 1
            continue
        frame = frame[frame["delivery_date"] == day].groupby(["delivery_date", "period", "eic"], as_index=False)["mw"].sum()
        frame.to_csv(path, index=False, date_format="%Y-%m-%d")
        stats["saved"] += 1
    return stats


def load(folder: Path, eic_units: dict[str, str], units: set[str], start: pd.Timestamp) -> pd.DataFrame:
    """Read the saved days from start as MW per quarter-hour of the wanted units."""
    files = [path for path in sorted(folder.glob("*.csv")) if pd.Timestamp(path.stem) >= start]
    if not files:
        return pd.DataFrame(columns=KEY + ["mw"])
    frame = pd.concat([pd.read_csv(path, parse_dates=["delivery_date"]) for path in files], ignore_index=True)
    frame["unit"] = frame["eic"].map(eic_units)
    frame = frame[frame["unit"].isin(units)].groupby(KEY, as_index=False)["mw"].sum()
    # a few units report hourly: carry each hour's value over its four quarters
    grid = frame[["delivery_date", "unit"]].drop_duplicates().merge(pd.DataFrame({"period": range(96)}), how="cross")
    frame = grid.merge(frame, on=KEY, how="left").sort_values(["unit", "delivery_date", "period"])
    frame["mw"] = frame.groupby(["unit", "delivery_date"])["mw"].ffill(limit=3)
    return frame.reset_index(drop=True)


def _shift(frame: pd.DataFrame, days: int) -> pd.DataFrame:
    return frame.assign(delivery_date=frame["delivery_date"] + pd.Timedelta(days=days))


def unit_inputs(output: pd.DataFrame, plant_of: pd.Series, known_until: int) -> tuple[pd.DataFrame, pd.DataFrame]:
    """Make each delivery day's inputs from output published before a day-ahead prediction:
    the whole day two days before, and the day before up to quarter known_until.

    Returns the per-quarter inputs and the per-day inputs. A plant is the units sharing plant_of: sisters swap who is redispatched.
    """
    output = output.assign(plant=output["unit"].map(plant_of))
    plant = output.groupby(["delivery_date", "period", "plant"], as_index=False)["mw"].sum().rename(columns={"mw": "plant_gen_2d_mw"})
    quarters = output.merge(plant, on=["delivery_date", "period", "plant"], how="left").rename(columns={"mw": "unit_gen_2d_mw"})
    quarters = _shift(quarters[KEY + QUARTER_COLUMNS], 2)

    before = output.groupby(["delivery_date", "unit", "plant"], as_index=False)["mw"].mean().rename(columns={"mw": "unit_gen_2d_day_mw"})
    plant_day = before.groupby(["delivery_date", "plant"])["unit_gen_2d_day_mw"]
    before["unit_share_2d"] = (before["unit_gen_2d_day_mw"] / plant_day.transform("sum")).where(plant_day.transform("sum") > 0)
    before["unit_rank_2d"] = plant_day.rank(ascending=False, method="min")
    before = _shift(before.drop(columns="plant"), 2)

    known = output[output["period"] < known_until]
    yesterday = known.groupby(["delivery_date", "unit", "plant"], as_index=False)["mw"].mean().rename(columns={"mw": "unit_gen_1d_morning_mw"})
    yesterday["plant_gen_1d_morning_mw"] = yesterday.groupby(["delivery_date", "plant"])["unit_gen_1d_morning_mw"].transform("sum")
    yesterday["sisters_gen_1d_morning_mw"] = yesterday["plant_gen_1d_morning_mw"] - yesterday["unit_gen_1d_morning_mw"]
    last_hour = known[known["period"] >= known_until - 4].groupby(["delivery_date", "unit"], as_index=False)["mw"].mean()
    yesterday = yesterday.drop(columns="plant").merge(last_hour.rename(columns={"mw": "unit_gen_1d_noon_mw"}), on=["delivery_date", "unit"], how="left")
    yesterday = _shift(yesterday, 1)

    days = before.merge(yesterday, on=["delivery_date", "unit"], how="outer")
    return quarters.reset_index(drop=True), days[["delivery_date", "unit"] + DAY_COLUMNS].reset_index(drop=True)
