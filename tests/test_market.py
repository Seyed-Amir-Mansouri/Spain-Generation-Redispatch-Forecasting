import io
import urllib.error
from datetime import date

import pandas as pd

from congestion_es import market, omie


def _file(rows):
    """Build an OMIE marginal price file from (period, portugal, spain) rows."""
    lines = ["MARGINALPIBC;"] + [f"2026;07;01;{p};{pt};{es};" for p, pt, es in rows] + ["*"]
    return "\n".join(lines)


def test_parse_quarter_hours_into_hourly_spanish_means():
    text = _file([(1, 10.0, 1.0), (2, 10.0, 2.0), (3, 10.0, 3.0), (4, 10.0, 4.0), (5, 10.0, 8.0), (96, 10.0, 6.0)])
    frame = omie.parse(text, date(2026, 7, 1))
    assert frame.set_index("hour")["price"].to_dict() == {0: 2.5, 1: 8.0, 23: 6.0}


def test_parse_ida3_periods_start_at_noon():
    text = _file([(49, 0.0, 5.0), (96, 0.0, 7.0)])
    assert omie.parse(text, date(2026, 7, 1))["hour"].tolist() == [12, 23]


def test_download_retries_a_temporary_failure():
    class Response(io.BytesIO):
        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

    calls = []

    def opener(request, timeout):
        calls.append(1)
        if len(calls) == 1:
            raise urllib.error.HTTPError(request.full_url, 503, "busy", None, None)
        return Response(_file([(1, 0, 1.0)]).encode("latin-1"))

    assert omie._download(date(2026, 7, 1), 1, opener=opener, sleep=lambda s: None).startswith("MARGINALPIBC")
    assert len(calls) == 2


def test_fetch_combines_sessions_per_hour():
    files = {1: _file([(1, 0, 1.0)]), 2: _file([(1, 0, 2.0)]), 3: None}
    frame = omie.fetch_intraday([date(2026, 7, 1)], download=lambda day, session: files[session], sleep=lambda s: None)
    row = frame.iloc[0]
    assert (row["ida1_price"], row["ida2_price"]) == (1.0, 2.0)
    assert pd.isna(row["ida3_price"])


def test_upsert_accepts_values_held_as_objects(tmp_path):
    path = tmp_path / "market.csv"
    market.upsert(path, pd.DataFrame({"delivery_date": ["2026-07-01"], "hour": [0], "ida1_price": [10.0]}))
    new = pd.DataFrame({"delivery_date": ["2026-07-01"], "hour": [0], "ida1_price": pd.Series([25.8], dtype=object)})
    assert market.upsert(path, new)["ida1_price"].tolist() == [25.8]


def test_upsert_updates_only_the_given_columns(tmp_path):
    path = tmp_path / "market.csv"
    market.upsert(path, pd.DataFrame({"delivery_date": ["2026-07-01"], "hour": [0], "da_price": [50.0], "ida1_price": [None]}))
    frame = market.upsert(path, pd.DataFrame({"delivery_date": ["2026-07-01", "2026-07-01"], "hour": [0, 1], "ida1_price": [48.0, 47.0]}))
    assert frame["da_price"].tolist()[0] == 50.0
    assert frame["ida1_price"].tolist() == [48.0, 47.0]
