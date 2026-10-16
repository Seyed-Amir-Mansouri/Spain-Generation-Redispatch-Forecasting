import io
import zipfile
from datetime import date

import pandas as pd

from congestion_es import i90


def test_period_hours_for_hourly_and_quarter_hour_days():
    assert list(i90.period_hours(date(2025, 3, 1), 24)) == list(range(24))
    quarter = i90.period_hours(date(2026, 7, 1), 96)
    assert list(quarter[:5]) == [0, 0, 0, 0, 1]
    assert list(quarter[-1:]) == [23]


def test_period_hours_skip_and_repeat_on_clock_changes():
    spring = list(i90.period_hours(date(2025, 3, 30), 23))
    assert 2 not in spring and spring[:3] == [0, 1, 3]
    autumn = list(i90.period_hours(date(2025, 10, 26), 25))
    assert autumn.count(2) == 2


def _hourly_rows():
    """Rows shaped like the legacy Excel sheet, with hourly period labels and a Total column."""
    header = ["Redespacho", "Sentido", "Unidad de Programación", "Nº Oferta asignada", "Tipo Oferta",
              "Tipo cálculo", "Tipo Restricción", "Hora", "Total"] + [f"{h:02d}-{h + 1:02d}" for h in range(24)]
    unit = ["UPOPVPV", "Bajar", "FVX1", 1.0, 1.0, "Simple", "SCA", "", -30.0] + [-10.0, -20.0] + [""] * 22
    blank = ["UPOPVPV", "Subir", "CCG1", 1.0, 1.0, "Simple", "SOT", "", 0.0] + [""] * 24
    return [["", "", "", "", "", "", "", "Indicadores", "MWh"], header, unit, blank]


def test_parse_hourly_sheet_keeps_abs_values_and_drops_empty_rows():
    frame = i90.parse_rows(_hourly_rows(), date(2025, 3, 1))
    assert frame[["unit", "direction", "restriction"]].drop_duplicates().values.tolist() == [["FVX1", "Bajar", "SCA"]]
    assert frame.set_index("hour")["mwh"].to_dict() == {0: 10.0, 1: 20.0}


def test_parse_quarter_hour_csv_sums_to_hours_with_decimal_commas():
    header = "Redespacho;Sentido;Unidad de Programación;Tipo Oferta;Tipo cálculo;Tipo Restricción;Cuarto de Hora del dia;" + ";".join(str(i) for i in range(1, 97))
    values = ["1,250", "1,250", "1,250", "1,250"] + ["0,000"] * 92
    row = "UPOPVPV;Bajar;EOL1;1;Simple;SCB;;" + ";".join(values)
    text = "\n".join([";;;;;;Indicadores;MWh", ";;;;;;Dia;01-julio-2026", header, row])
    frame = i90.parse_rows(i90._csv_rows(text.encode("utf-8")), date(2026, 7, 1))
    assert frame["hour"].tolist() == [0]
    assert frame["mwh"].tolist() == [5.0]


def test_extract_reads_the_phase_one_csv_from_a_zip():
    header = "Redespacho;Sentido;Unidad de Programación;Tipo Oferta;Tipo cálculo;Tipo Restricción;Cuarto de Hora del dia;" + ";".join(str(i) for i in range(1, 97))
    row = "UPOPVPV;Bajar;EOL1;1;Simple;SCB;;" + ";".join(["2,0"] + ["0"] * 95)
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as bundle:
        bundle.writestr("I90DIA03_20260701.csv", "\n".join([";;", header, row]))
        bundle.writestr("I90DIA08_20260701.csv", "unrelated")
    frame = i90.extract(buffer.getvalue(), date(2026, 7, 1))
    assert frame["mwh"].sum() == 2.0


def test_load_filters_direction_and_types_and_lists_days(tmp_path):
    rows = pd.DataFrame({
        "delivery_date": ["2026-07-01"] * 3, "hour": [0, 0, 1], "unit": ["A", "B", "C"],
        "direction": ["Bajar", "Subir", "Bajar"], "restriction": ["SCA", "SCA", "SOT"], "mwh": [1.0, 2.0, 3.0],
    })
    rows.to_csv(i90.day_path(tmp_path, date(2026, 7, 1)), index=False, compression="gzip")
    frame, days = i90.load(tmp_path, "Bajar", ("SCA", "SCB"))
    assert frame["unit"].tolist() == ["A"]
    assert days == [pd.Timestamp("2026-07-01")]
