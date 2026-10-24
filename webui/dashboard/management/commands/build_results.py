"""Rebuild data/results.json from a rolling backtest table (one row per day, quarter-hour and unit, as the project's backtest saves it).
Unit names, technologies and places are kept from the current results.json. Needs pandas.
usage: manage.py build_results PATH_TO_BACKTEST.pkl [--source TEXT]"""
import json
from pathlib import Path

from django.conf import settings
from django.core.management.base import BaseCommand


class Command(BaseCommand):
    help = "Rebuild data/results.json from a rolling backtest table."

    def add_arguments(self, parser):
        parser.add_argument("backtest", type=Path)
        parser.add_argument("--source", help="Text shown in the page footer; defaults to the current one.")

    def handle(self, backtest, source=None, **options):
        import pandas as pd

        current = json.loads(settings.RESULTS_FILE.read_text(encoding="utf-8"))
        known = {u["u"]: u for u in current["units"]}
        frame = pd.read_pickle(backtest)
        frame["hour"] = frame["period"] // 4
        days = sorted(frame["delivery_date"].unique())
        units = sorted(frame["unit"].unique())
        records = []
        for (day, unit), g in frame.groupby(["delivery_date", "unit"]):
            hourly = g.groupby("hour")
            records.append({
                "u": unit, "d": days.index(day),
                "pu": round(float(g["up_probability"].max()), 4), "pd": round(float(g["down_probability"].max()), 4),
                "qu": int((g["up_target"] == 1).sum()), "qd": int((g["down_target"] == 1).sum()),
                "hu": [round(float(v), 3) for v in hourly["up_probability"].max().reindex(range(24), fill_value=0)],
                "hd": [round(float(v), 3) for v in hourly["down_probability"].max().reindex(range(24), fill_value=0)],
                "au": _bits(g.loc[g["up_target"] == 1, "hour"]), "ad": _bits(g.loc[g["down_target"] == 1, "hour"]),
            })
        out = {
            "days": [f"{pd.Timestamp(d):%Y-%m-%d}" for d in days],
            "units": [known.get(u, {"u": u, "name": u, "tech": "Other", "lat": None, "lon": None}) for u in units],
            "records": records,
            "source": source or current["source"],
        }
        settings.RESULTS_FILE.write_text(json.dumps(out, separators=(",", ":"), ensure_ascii=False), encoding="utf-8")
        self.stdout.write(f"{len(records)} unit-days over {len(days)} days written to {settings.RESULTS_FILE}")


def _bits(hours):
    # one bit per hour in which the unit was moved
    return sum(1 << int(h) for h in set(hours))
