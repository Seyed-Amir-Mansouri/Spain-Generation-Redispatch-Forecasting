from __future__ import annotations

import math

import pandas as pd

from .locations import CROSS_BORDER_TYPE, fold

TECHNOLOGY_GROUPS = {
    "solar": ("solar",),
    "wind": ("eolic",),
    "hydro": ("hidr", "fluyente", "embalse", "bombeo"),
    "thermal": ("ciclo", "gas", "vapor", "cogener", "nuclear", "carbon", "hulla", "fuel", "petroleo"),
}
CAPACITY_COLUMNS = [f"mw_{group}" for group in (*TECHNOLOGY_GROUPS, "other")]


def cell_id(lat: float, lon: float, size: float) -> str:
    """Name the grid cell containing a point by its south-west corner."""
    south = math.floor(lat / size) * size
    west = math.floor(lon / size) * size
    return f"{south:.2f}_{west:.2f}"


def cell_center(area: str, size: float) -> tuple[float, float]:
    """Return the centre coordinates of a grid cell."""
    south, west = (float(part) for part in area.split("_"))
    return south + size / 2, west + size / 2


def _group(technology) -> str:
    """Assign an REE production type to a coarse technology group."""
    text = fold(technology)
    for group, words in TECHNOLOGY_GROUPS.items():
        if any(word in text for word in words):
            return group
    return "other"


def located(units: pd.DataFrame, size: float) -> pd.DataFrame:
    """Keep located physical units and tag each with its grid cell."""
    frame = units[units["lat"].notna()].copy()
    frame["area"] = [cell_id(lat, lon, size) for lat, lon in zip(frame["lat"], frame["lon"])]
    return frame


def unit_shares(units: pd.DataFrame, programming: pd.DataFrame, size: float) -> pd.DataFrame:
    """Split each programming unit across grid cells in proportion to its located physical capacity."""
    capacity = units.assign(mw=units["max_mw"].fillna(0).clip(lower=0))
    totals = capacity.groupby("programming_unit")["mw"].sum()
    cross_border = set(programming.loc[programming["unit_type"] == CROSS_BORDER_TYPE, "code"])
    frame = located(capacity, size)
    frame = frame[~frame["programming_unit"].isin(cross_border)]
    shares = frame.groupby(["programming_unit", "area"], as_index=False)["mw"].sum()
    shares["share"] = shares["mw"] / shares["programming_unit"].map(totals)
    shares = shares[shares["share"] > 0]
    return shares.rename(columns={"programming_unit": "unit"})[["unit", "area", "share"]]


def attribute(redispatch: pd.DataFrame, shares: pd.DataFrame) -> pd.DataFrame:
    """Distribute unit redispatch to grid cells and sum MWh per cell and hour."""
    merged = redispatch.merge(shares, on="unit", how="inner")
    merged["mwh"] = merged["mwh"] * merged["share"]
    return merged.groupby(["delivery_date", "hour", "area"], as_index=False)["mwh"].sum()


def coverage(redispatch: pd.DataFrame, shares: pd.DataFrame) -> float:
    """Return the share of redispatched MWh that could be placed in a grid cell."""
    total = redispatch["mwh"].sum()
    if not total:
        return float("nan")
    return attribute(redispatch, shares)["mwh"].sum() / total


def capacity_by_area(units: pd.DataFrame, size: float, min_mw: float) -> pd.DataFrame:
    """Sum located capacity per grid cell and technology group, keeping cells above a minimum."""
    frame = located(units, size)
    frame["group"] = frame["technology"].map(_group)
    table = frame.pivot_table(index="area", columns="group", values="max_mw", aggfunc="sum", fill_value=0.0)
    table = table.reindex(columns=[column[3:] for column in CAPACITY_COLUMNS], fill_value=0.0)
    table.columns = CAPACITY_COLUMNS
    table["mw_total"] = table.sum(axis=1)
    table = table[table["mw_total"] >= min_mw].reset_index()
    centers = [cell_center(area, size) for area in table["area"]]
    table["cell_lat"] = [c[0] for c in centers]
    table["cell_lon"] = [c[1] for c in centers]
    return table
