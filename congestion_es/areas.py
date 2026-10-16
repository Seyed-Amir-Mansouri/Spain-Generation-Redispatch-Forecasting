from __future__ import annotations

import math

import numpy as np

import pandas as pd

from .locations import CROSS_BORDER_TYPE, fold

TECHNOLOGY_GROUPS = {
    "solar": ("solar",),
    "wind": ("eolic",),
    "hydro": ("hidr", "fluyente", "embalse", "bombeo"),
    "thermal": ("ciclo", "gas", "vapor", "cogener", "nuclear", "carbon", "hulla", "fuel", "petroleo"),
}
CAPACITY_COLUMNS = [f"mw_{group}" for group in (*TECHNOLOGY_GROUPS, "other")]
TECHNOLOGY_COLUMN = {"solar": "mw_solar", "wind": "mw_wind", "hydro": "mw_hydro", "thermal": "mw_thermal"}


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


def estimate_shares(
    redispatch: pd.DataFrame,
    shares: pd.DataFrame,
    capacity: pd.DataFrame,
    technology: dict[str, str],
    excluded: set[str],
    top: int = 3,
    prior_weight: float = 0.02,
) -> pd.DataFrame:
    """Share each unit's unplaced redispatch among the cells whose redispatch hours it follows most closely."""
    key = ["delivery_date", "hour"]
    placed = shares.groupby("unit")["share"].sum()
    remaining = (1 - placed.reindex(redispatch["unit"].unique()).fillna(0)).clip(lower=0)
    remaining = remaining[(remaining > 0.01) & ~remaining.index.isin(excluded)]
    if remaining.empty:
        return pd.DataFrame(columns=["unit", "area", "share"])
    cells = attribute(redispatch, shares).pivot_table(index=key, columns="area", values="mwh", aggfunc="sum").fillna(0.0)
    units = redispatch[redispatch["unit"].isin(remaining.index)].pivot_table(index=key, columns="unit", values="mwh", aggfunc="sum")
    units = units.reindex(cells.index.union(units.index)).fillna(0.0)
    cells = cells.reindex(units.index).fillna(0.0)
    standard = lambda frame: (frame - frame.mean()) / frame.std().replace(0, 1)
    correlation = standard(units).T @ standard(cells) / len(units)
    by_cell = capacity.set_index("area")
    rows = []
    for unit, row in correlation.iterrows():
        column = TECHNOLOGY_COLUMN.get(_group(technology.get(unit, "")), "mw_total")
        prior = np.log1p(by_cell[column].reindex(row.index).fillna(0.0))
        score = (row + prior_weight * prior).nlargest(top)
        weights = score.clip(lower=1e-6)
        for area, weight in (weights / weights.sum()).items():
            rows.append({"unit": unit, "area": area, "share": remaining[unit] * weight})
    return pd.DataFrame(rows)


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
