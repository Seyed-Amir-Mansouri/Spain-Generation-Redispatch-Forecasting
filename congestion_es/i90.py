from __future__ import annotations

import io
import re
import unicodedata
import zipfile
from datetime import date
from pathlib import Path

import numpy as np
import pandas as pd

ARCHIVE_ID = 34
PHASE_ONE_SHEET = "03"
TIMEZONE = "Europe/Madrid"
COLUMNS = ["delivery_date", "hour", "unit", "direction", "restriction", "mwh"]


def _fold(text) -> str:
    """Lowercase and strip accents so header names match regardless of encoding."""
    return unicodedata.normalize("NFKD", str(text)).encode("ascii", "ignore").decode().strip().lower()


def _number(value) -> float:
    """Parse a cell that may be blank, a float, or a decimal-comma string."""
    if isinstance(value, (int, float)):
        return float(value)
    text = str(value).strip()
    if not text:
        return 0.0
    if "," in text:
        text = text.replace(".", "").replace(",", ".")
    return float(text)


def _is_period(header) -> bool:
    """Return whether a header cell labels an hourly ("00-01") or quarter-hour ("1") period."""
    if isinstance(header, (int, float)):
        return True
    text = str(header).strip()
    return text.isdigit() or bool(re.match(r"^\d{1,2}-\d{1,2}", text))


def period_hours(day: date, periods: int) -> np.ndarray:
    """Map each period of a delivery day to its local clock hour, handling clock changes."""
    step = 15 if periods > 30 else 60
    start = pd.Timestamp(day).tz_localize(TIMEZONE).tz_convert("UTC")
    instants = start + pd.to_timedelta(np.arange(periods) * step, unit="min")
    return instants.tz_convert(TIMEZONE).hour.to_numpy()


def parse_rows(rows: list[list], day: date) -> pd.DataFrame:
    """Turn the rows of a redispatch sheet into hourly MWh per unit, direction and restriction type."""
    start = next((i for i, row in enumerate(rows) if row and _fold(row[0]) == "redespacho"), None)
    if start is None:
        return pd.DataFrame(columns=COLUMNS)
    header = [_fold(cell) for cell in rows[start]]
    unit_col = header.index("unidad de programacion")
    direction_col = header.index("sentido")
    restriction_col = header.index("tipo restriccion")
    period_cols = [i for i, cell in enumerate(rows[start]) if _is_period(cell)]
    hours = period_hours(day, len(period_cols))

    records = []
    for row in rows[start + 1:]:
        if len(row) <= max(period_cols) or not str(row[unit_col]).strip():
            continue
        values = np.abs([_number(row[i]) for i in period_cols])
        if not values.any():
            continue
        unit = str(row[unit_col]).strip()
        direction = str(row[direction_col]).strip()
        restriction = str(row[restriction_col]).strip()
        for hour in np.unique(hours):
            mwh = values[hours == hour].sum()
            if mwh > 0:
                records.append((pd.Timestamp(day), int(hour), unit, direction, restriction, mwh))
    frame = pd.DataFrame(records, columns=COLUMNS)
    return frame.groupby(COLUMNS[:-1], as_index=False)["mwh"].sum()


def _csv_rows(raw: bytes) -> list[list]:
    """Split a semicolon-separated sheet into rows of strings."""
    text = raw.decode("utf-8-sig", errors="replace")
    return [line.split(";") for line in text.splitlines()]


def _xls_rows(raw: bytes, sheet: str) -> list[list]:
    """Read one sheet of a legacy Excel workbook into rows of cell values."""
    import xlrd

    book = xlrd.open_workbook(file_contents=raw, on_demand=True, logfile=io.StringIO())
    table = book.sheet_by_name(sheet)
    return [table.row_values(r) for r in range(table.nrows)]


def extract(archive: bytes, day: date, sheet: str = PHASE_ONE_SHEET) -> pd.DataFrame:
    """Extract one redispatch sheet from a downloaded I90DIA zip in either CSV or Excel form."""
    name = f"I90DIA{sheet}"
    with zipfile.ZipFile(io.BytesIO(archive)) as bundle:
        members = bundle.namelist()
        csv_member = next((m for m in members if m.upper().startswith(name) and m.lower().endswith(".csv")), None)
        if csv_member:
            return parse_rows(_csv_rows(bundle.read(csv_member)), day)
        xls_member = next(m for m in members if m.lower().endswith(".xls"))
        return parse_rows(_xls_rows(bundle.read(xls_member), name), day)


def day_path(folder: Path, day: date) -> Path:
    """Return the cached redispatch file for one delivery day."""
    return folder / f"i90_{day:%Y%m%d}.csv.gz"


def download(client, days, folder: Path, log=print) -> dict:
    """Download and extract each missing day, keeping only the compact redispatch table."""
    folder.mkdir(parents=True, exist_ok=True)
    stats = {"downloaded": 0, "cached": 0, "unavailable": 0}
    for day in days:
        target = day_path(folder, day)
        if target.exists():
            stats["cached"] += 1
            continue
        archive = client.archive(ARCHIVE_ID, day)
        if archive is None:
            stats["unavailable"] += 1
            log(f"{day}: not published")
            continue
        frame = extract(archive, day)
        frame.to_csv(target, index=False, compression="gzip")
        stats["downloaded"] += 1
        log(f"{day}: {len(frame)} rows")
    return stats


def load(folder: Path, direction: str, restrictions: tuple[str, ...]) -> tuple[pd.DataFrame, list[pd.Timestamp]]:
    """Load cached redispatch rows for one direction and set of restriction types, plus the days covered."""
    frames, days = [], []
    for path in sorted(folder.glob("i90_*.csv.gz")):
        days.append(pd.Timestamp(path.name[4:12]))
        frame = pd.read_csv(path, parse_dates=["delivery_date"])
        frames.append(frame[(frame["direction"] == direction) & frame["restriction"].isin(restrictions)])
    if not frames:
        return pd.DataFrame(columns=COLUMNS), days
    return pd.concat(frames, ignore_index=True), days
