import io
import json
from datetime import date

import pandas as pd

from congestion_es import esios, market


class FakeResponse(io.BytesIO):
    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False


def test_client_waits_between_requests():
    now = [0.0]
    sleeps = []

    def opener(request, timeout):
        return FakeResponse(b"{}")

    def sleep(seconds):
        sleeps.append(seconds)
        now[0] += seconds

    client = esios.Client("key", 5, opener=opener, sleep=sleep, clock=lambda: now[0])
    client.get("/a")
    now[0] += 2
    client.get("/b")
    assert sleeps == [3]


def test_archive_returns_none_when_no_file_is_published():
    body = json.dumps({"message": "No values for specified archive"}).encode()
    client = esios.Client("key", 0, opener=lambda request, timeout: FakeResponse(body))
    assert client.archive(34, date(2026, 10, 1)) is None


class FakeIndicatorClient:
    def indicator(self, indicator_id, start, end):
        if indicator_id == 2:
            return esios.quarter_values([])
        return esios.quarter_values([{"value": -4.0, "datetime": f"{start}T05:00:00.000+02:00", "geo_name": "Península"}])


def test_constraints_tolerate_an_indicator_with_no_values():
    frame = market.fetch_constraints(FakeIndicatorClient(), {"a": 1, "b": 2}, pd.Timestamp("2026-07-01"), pd.Timestamp("2026-07-02"))
    assert len(frame) == 96
    assert frame.loc[frame["period"] == 20, "a"].tolist() == [4.0]
    assert frame["b"].eq(0).all()


def test_quarter_values_keep_peninsular_quarter_hours_in_local_time():
    values = [
        {"value": 1.5, "datetime": "2025-01-15T10:00:00.000+01:00", "geo_name": "Península"},
        {"value": 1.5, "datetime": "2025-01-15T10:15:00.000+01:00", "geo_name": "Península"},
        {"value": 9.0, "datetime": "2025-01-15T10:15:00.000+01:00", "geo_name": "Baleares"},
        {"value": 2.0, "datetime": "2025-01-15T11:00:00.000+01:00", "geo_name": "Península"},
    ]
    frame = esios.quarter_values(values)
    assert frame["period"].tolist() == [40, 41, 44]
    assert frame["value"].tolist() == [1.5, 1.5, 2.0]
