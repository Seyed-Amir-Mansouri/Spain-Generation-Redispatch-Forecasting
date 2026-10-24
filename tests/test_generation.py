import pandas as pd

from congestion_es import generation

DOCUMENT = """<GL_MarketDocument>
  <TimeSeries>
    <registeredResource.mRID codingScheme="A01">UNIT-A</registeredResource.mRID>
    <MktPSRType><PowerSystemResources><mRID codingScheme="A01">PLANT-A1</mRID><name>A ONE</name></PowerSystemResources></MktPSRType>
    <Period>
      <timeInterval><start>2026-01-14T23:00Z</start><end>2026-01-15T00:00Z</end></timeInterval>
      <resolution>PT15M</resolution>
      <Point><position>1</position><quantity>10</quantity></Point>
      <Point><position>3</position><quantity>30</quantity></Point>
    </Period>
  </TimeSeries>
  <TimeSeries>
    <MktPSRType><PowerSystemResources><mRID codingScheme="A01">PLANT-A2</mRID></PowerSystemResources></MktPSRType>
    <Period>
      <timeInterval><start>2026-01-14T23:00Z</start><end>2026-01-15T00:00Z</end></timeInterval>
      <resolution>PT60M</resolution>
      <Point><position>1</position><quantity>5</quantity></Point>
    </Period>
  </TimeSeries>
</GL_MarketDocument>"""


def test_parse_fills_left_out_positions_and_adds_plants_up_per_unit():
    frame = generation.parse(DOCUMENT, {"PLANT-A2": "UNIT-A"})
    assert frame["delivery_date"].eq(pd.Timestamp("2026-01-15")).all()
    assert frame.set_index("period")["mw"].to_dict() == {0: 15.0, 1: 10.0, 2: 30.0, 3: 30.0}
    assert set(frame["eic"]) == {"UNIT-A"}


def test_parse_skips_plants_without_a_known_unit():
    assert len(generation.parse(DOCUMENT, {})) == 4


def test_plant_codes_come_from_series_that_name_their_unit():
    codes = generation.plant_codes(DOCUMENT)
    assert codes.to_dict("records") == [{"plant_eic": "PLANT-A1", "eic": "UNIT-A", "name": "A ONE"}]


def test_inputs_use_only_output_out_before_a_noon_prediction():
    days = pd.date_range("2026-03-01", periods=3, freq="D")
    rows = [(day, period, unit, mw * (i + 1)) for i, day in enumerate(days) for period in range(96)
            for unit, mw in (("A1", 100.0), ("A2", 300.0), ("B", 50.0))]
    output = pd.DataFrame(rows, columns=["delivery_date", "period", "unit", "mw"])
    output.loc[(output["delivery_date"] == days[1]) & (output["period"] >= 48), "mw"] = 9999.0
    plant_of = pd.Series({"A1": "A", "A2": "A", "B": "B"})
    quarters, daily = generation.unit_inputs(output, plant_of, known_until=48)

    q = quarters.set_index(["delivery_date", "period", "unit"])
    assert q.loc[(days[2], 5, "A1"), "unit_gen_2d_mw"] == 100.0
    assert q.loc[(days[2], 5, "A1"), "plant_gen_2d_mw"] == 400.0

    d = daily.set_index(["delivery_date", "unit"])
    assert d.loc[(days[2], "A1"), "unit_share_2d"] == 0.25
    assert d.loc[(days[2], "A2"), "unit_rank_2d"] == 1
    assert d.loc[(days[2], "A1"), "unit_gen_1d_morning_mw"] == 200.0
    assert d.loc[(days[2], "A1"), "unit_gen_1d_noon_mw"] == 200.0
    assert d.loc[(days[2], "A1"), "sisters_gen_1d_morning_mw"] == 600.0
    assert d.loc[(days[2], "B"), "plant_gen_1d_morning_mw"] == 100.0


def test_load_carries_hourly_values_over_their_quarters(tmp_path):
    pd.DataFrame({"delivery_date": "2026-03-01", "period": [0, 4], "eic": "E1", "mw": [8.0, 12.0]}).to_csv(tmp_path / "2026-03-01.csv", index=False)
    frame = generation.load(tmp_path, {"E1": "U1"}, {"U1"}, pd.Timestamp("2026-03-01"))
    assert frame["mw"].head(8).tolist() == [8.0] * 4 + [12.0] * 4
    assert frame["mw"].iloc[8:].isna().all()
