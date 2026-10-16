import pandas as pd

from congestion_es import areas


def _units():
    """Two plants of one portfolio in different cells, one unlocated plant and a cross-border unit."""
    return pd.DataFrame({
        "physical_unit": ["A1", "A2", "A3", "X1"],
        "programming_unit": ["PORT", "PORT", "PORT", "XFRA"],
        "technology": ["Solar fotovoltaica", "Eólica terrestre", "Solar fotovoltaica", "Importación Francia"],
        "max_mw": [60.0, 30.0, 10.0, 1000.0],
        "lat": [38.2, 41.7, None, 43.0],
        "lon": [-6.3, -1.2, None, -1.8],
    })


def _programming():
    """Registry rows marking XFRA as a cross-border unit."""
    return pd.DataFrame({"code": ["PORT", "XFRA"], "unit_type": ["Generación", "Unidad instrumental internacional"]})


def test_cell_id_and_center_round_trip():
    area = areas.cell_id(38.2, -6.3, 0.5)
    assert area == "38.00_-6.50"
    assert areas.cell_center(area, 0.5) == (38.25, -6.25)


def test_shares_split_by_located_capacity_and_skip_cross_border_units():
    shares = areas.unit_shares(_units(), _programming(), 0.5).set_index("area")["share"]
    assert set(shares.index) == {"38.00_-6.50", "41.50_-1.50"}
    assert round(shares["38.00_-6.50"], 3) == 0.6
    assert round(shares["41.50_-1.50"], 3) == 0.3


def test_attribute_and_coverage():
    shares = areas.unit_shares(_units(), _programming(), 0.5)
    redispatch = pd.DataFrame({
        "delivery_date": pd.to_datetime(["2026-07-01", "2026-07-01"]), "hour": [12, 12],
        "unit": ["PORT", "XFRA"], "mwh": [100.0, 50.0],
    })
    attributed = areas.attribute(redispatch, shares).set_index("area")["mwh"]
    assert attributed.to_dict() == {"38.00_-6.50": 60.0, "41.50_-1.50": 30.0}
    assert round(areas.coverage(redispatch, shares), 3) == 0.6


def test_capacity_by_area_groups_technologies_and_applies_minimum():
    table = areas.capacity_by_area(_units(), 0.5, min_mw=50).set_index("area")
    assert list(table.index) == ["38.00_-6.50", "43.00_-2.00"]
    assert table.loc["38.00_-6.50", "mw_solar"] == 60.0
    assert table.loc["43.00_-2.00", "mw_other"] == 1000.0
