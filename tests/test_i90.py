import io
import zipfile
from datetime import date

import pandas as pd

from congestion_es import i90


def test_period_slots_number_quarters_of_the_local_day():
    slots = i90.period_slots(date(2026, 7, 1), 96)
    assert list(slots[:5]) == [0, 1, 2, 3, 4]
    assert list(slots[-1:]) == [95]


def test_period_slots_skip_and_repeat_on_clock_changes():
    spring = list(i90.period_slots(date(2025, 3, 30), 92))
    assert 8 not in spring and spring[7:9] == [7, 12]
    autumn = list(i90.period_slots(date(2025, 10, 26), 100))
    assert autumn.count(8) == 2


def test_hourly_sheets_are_rejected():
    header = ["Redespacho", "Sentido", "Unidad de Programación", "Tipo Restricción", "Total"] + [f"{h:02d}-{h + 1:02d}" for h in range(24)]
    rows = [header, ["UPOPVPV", "Bajar", "FVX1", "SCA", -30.0] + [-10.0, -20.0] + [""] * 22]
    try:
        i90.parse_rows(rows, date(2025, 3, 1))
    except ValueError:
        return
    raise AssertionError("an hourly sheet was accepted")


def test_parse_quarter_hour_csv_keeps_quarters_with_decimal_commas():
    header = "Redespacho;Sentido;Unidad de Programación;Tipo Oferta;Tipo cálculo;Tipo Restricción;Cuarto de Hora del dia;" + ";".join(str(i) for i in range(1, 97))
    values = ["-1,250", "0,000", "2,500"] + ["0,000"] * 93
    rows = ["UPOPVPV;Bajar;EOL1;1;Simple;SCB;;" + ";".join(values), "UPOPVPV;Subir;CCG1;1;Simple;SOT;;" + ";".join(["0"] * 96)]
    text = "\n".join([";;;;;;Indicadores;MWh", ";;;;;;Dia;01-julio-2026", header, *rows])
    frame = i90.parse_rows(i90._csv_rows(text.encode("utf-8")), date(2026, 7, 1))
    assert frame[["unit", "direction", "restriction"]].drop_duplicates().values.tolist() == [["EOL1", "Bajar", "SCB"]]
    assert frame.set_index("period")["mwh"].to_dict() == {0: 1.25, 2: 2.5}


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
        "delivery_date": ["2026-07-01"] * 3, "period": [0, 0, 1], "unit": ["A", "B", "C"],
        "direction": ["Bajar", "Subir", "Bajar"], "restriction": ["SCA", "SCA", "SOT"], "mwh": [1.0, 2.0, 3.0],
    })
    rows.to_csv(i90.day_path(tmp_path, date(2026, 7, 1)), index=False, compression="gzip")
    frame, days = i90.load(tmp_path, "Bajar", ("SCA", "SCB"))
    assert frame["unit"].tolist() == ["A"]
    assert days == [pd.Timestamp("2026-07-01")]
