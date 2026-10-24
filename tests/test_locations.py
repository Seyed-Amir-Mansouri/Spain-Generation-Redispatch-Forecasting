import pandas as pd

from redispatch_es import locations


def _reference(rows):
    """Build a reference plant table from (name, source, lat, lon) tuples."""
    frame = pd.DataFrame(rows, columns=["name", "source", "lat", "lon"]).assign(origin="osm", ref_mw=float("nan"))
    frame["key"] = frame["name"].map(locations.normalize_name)
    return frame


def test_normalize_drops_generic_words_and_converts_roman_numerals():
    assert locations.normalize_name("Parque Eólico Las Majas IV") == "majas 4"
    assert locations.normalize_name("CC BESOS 5 T GAS 1") == "besos 5 1"
    assert locations.normalize_name("CARTAGENA1") == "cartagena 1"


def test_hydro_labels_do_not_match_a_wind_farm_with_the_same_place_name():
    reference = _reference([("Parque Eólico La Vega", "wind", 41.5, -5.7)])
    matcher = locations.Matcher(reference)
    assert matcher.match(["LA VEGA 1"], "Fluyente") == (None, None)
    assert matcher.match(["LA VEGA 1"], "Eólica terrestre")[1] == "group"


def test_candidates_far_apart_are_rejected():
    reference = _reference([("Parque Eólico El Carrascal", "wind", 37.4, -2.5), ("Parque Eólico El Carrascal", "wind", 42.0, -1.0)])
    assert locations.Matcher(reference).match(["EOLICA EL CARRASCAL"], "Eólica terrestre") == (None, None)


def test_single_plant_units_are_also_matched_by_their_programming_unit_name():
    reference = _reference([("Termosolar Manchasol I", "solar", 39.2, -3.3)])
    physical = pd.DataFrame({
        "code": ["SMS1", "P1", "P2"], "short_name": ["SMS1", "P1", "P2"], "long_name": ["SMS1", "P1", "P2"],
        "technology": ["Solar térmica"] * 3, "programming_unit": ["SMASOL1", "PORT", "PORT"], "max_mw": [50.0, 20.0, 20.0],
    })
    programming = pd.DataFrame({"code": ["SMASOL1", "PORT"], "name": ["MANCHASOL I", "MANCHASOL I"]})
    result = locations.locate_units(physical, reference, pd.DataFrame(columns=["level", "code", "origin", "reference_name", "confidence"]), programming)
    assert result.set_index("physical_unit")["lat"].notna().to_dict() == {"SMS1": True, "P1": False, "P2": False}


def test_manual_override_wins_over_name_matching():
    reference = _reference([("Central Termica Bahia de Bizkaia Electricidad", "gas", 43.36, -3.09), ("Bizkaia Energia", "gas", 43.23, -2.75)])
    physical = pd.DataFrame({
        "code": ["BAHITG1"], "short_name": ["BAHITG1"], "long_name": ["BAHIAB TG1"],
        "technology": ["Ciclo combinado"], "programming_unit": ["BAHIAB"], "max_mw": [256.6],
    })
    manual = pd.DataFrame({
        "level": ["UP"], "code": ["BAHIAB"], "origin": ["osm"],
        "reference_name": ["Central Termica Bahia de Bizkaia Electricidad"], "confidence": ["high"], "evidence": [""],
    })
    result = locations.locate_units(physical, reference, manual)
    assert result.loc[0, "method"] == "manual"
    assert result.loc[0, "lat"] == 43.36
    assert result.loc[0, "confidence"] == "high"


def _sized(rows):
    """Build a reference plant table from (name, source, lat, lon, mw, origin) tuples."""
    frame = pd.DataFrame(rows, columns=["name", "source", "lat", "lon", "ref_mw", "origin"])
    frame["key"] = frame["name"].map(locations.normalize_name)
    return frame


def test_capacity_picks_between_namesakes_far_apart():
    reference = _sized([("Tagus", "solar", 39.64, -7.0, 330.0, "gem"), ("Tagus", "solar", 39.98, -3.53, 197.0, "gem")])
    matcher = locations.Matcher(reference)
    assert matcher.match(["TAGUS"], "Solar fotovoltaica", 305.7) == (0, "exact")
    assert matcher.match(["TAGUS"], "Solar fotovoltaica") == (None, None)


def test_cut_down_codes_match_by_prefix_only_when_capacity_agrees():
    reference = _sized([("Planta solar fotovoltaica Aurea", "solar", 39.1, -6.7, 171.0, "gem")])
    matcher = locations.Matcher(reference)
    assert matcher.match(["AUREAFV"], "Solar fotovoltaica", 150.0) == (0, "prefix")
    assert matcher.match(["AUREAFV"], "Solar fotovoltaica", 20.0) == (None, None)


def test_generic_place_words_never_match_by_prefix():
    reference = _sized([("Fuentes Alcarria", "solar", 40.9, -2.8, 40.0, "osm")])
    assert locations.Matcher(reference).match(["FUENTE2"], "Solar fotovoltaica", 41.3) == (None, None)


def test_gem_only_fills_units_that_other_sources_cannot_place():
    reference = _sized([
        ("Parque Eólico Celaya", "wind", 43.4, -7.5, float("nan"), "osm"),
        ("La Celaya", "wind", 37.3, -2.5, 28.8, "gem"),
        ("Gaetana", "solar", 39.1, -6.6, 171.0, "gem"),
    ])
    physical = pd.DataFrame({
        "code": ["CELAYA", "GAETANA"], "short_name": ["CELAYA", "GAETANA"], "long_name": ["CELAYA", "FGST152"],
        "technology": ["Eólica terrestre", "Solar fotovoltaica"], "programming_unit": ["UPA", "UPB"], "max_mw": [28.8, 150.0],
    })
    manual = pd.DataFrame(columns=["level", "code", "origin", "reference_name", "confidence"])
    result = locations.locate_units(physical, reference, manual).set_index("physical_unit")
    assert result.loc["CELAYA", "lat"] == 43.4
    assert result.loc["GAETANA", "lat"] == 39.1


def test_gem_pages_yield_name_alias_capacity_and_coordinates():
    text = (
        "'''Gaetana solar farm''' (Planta solar fotovoltaica Gaetana) is a solar photovoltaic (PV) farm.\n"
        "| 171 MWac<ref name=\"a\">{{cite web}}</ref>\n==Location==\n"
        "|-\n| La Roca de la Sierra, Badajoz, Extremadura, Spain<ref name=\"a\" />\n| 39.1022, -6.5742 (approximate)<ref name=\"a\" />\n"
    )
    rows = locations.parse_gem_page("Gaetana solar farm", text, "solar")
    assert rows == [{
        "name": "Gaetana", "aliases": "Planta solar fotovoltaica Gaetana", "source": "solar", "mw": 171.0,
        "location": "La Roca de la Sierra, Badajoz, Extremadura, Spain", "lat": 39.1022, "lon": -6.5742, "precision": "approximate",
    }]


def _substations(rows):
    """Build a substation table from (name, lat, lon) tuples."""
    frame = pd.DataFrame(rows, columns=["name", "lat", "lon"])
    frame["key"] = frame["name"].map(locations.normalize_name).str.replace(r"^(subestacion|set|se) ", "", regex=True)
    return frame


def test_codes_named_after_a_substation_are_placed_there():
    substations = _substations([("Subestación de Caparacena", 37.27, -3.73), ("SET Saladas", 38.3, -0.62)])
    assert locations.substation_match(["FCAPARA", "FCAPARA"], substations) == 0


def test_substation_is_refused_when_the_full_name_points_elsewhere():
    substations = _substations([("SET Saladas", 38.3, -0.62), ("Subestación El Palmar", 37.94, -1.19)])
    assert locations.substation_match(["FUENTE SALADA FASE 1", "FSALADA"], substations) is None
    assert locations.substation_match(["TS PALMA RIO I", "TSPALMAR"], substations) is None


def _places(rows):
    """Build a place table from (name, lat, lon) tuples."""
    frame = pd.DataFrame(rows, columns=["name", "lat", "lon"])
    frame["key"] = frame["name"].map(lambda n: " ".join(locations.fold(n).replace("-", " ").split()))
    return frame


def test_units_naming_a_municipality_are_placed_there():
    places = _places([("Fregenal de la Sierra", 38.17, -6.65), ("Brazatortas", 38.66, -4.29)])
    assert locations.place_match(["PS FREGENAL DE LA SIERRA"], places) == 0
    assert locations.place_match(["UF ELAWAN FOTOVOLTAICA BRAZATO"], places) == 1


def test_saints_and_landforms_are_not_mistaken_for_villages():
    places = _places([("Cristóbal", 40.47, -5.89), ("Cabrera de Mar", 41.53, 2.39), ("Gavilanes", 40.28, -4.85)])
    assert locations.place_match(["SAN CRISTOBAL 1"], places) is None
    assert locations.place_match(["P.E. SIERRA CABRERA"], places) is None
    assert locations.place_match(["P.E. SIERRA DE LOS GAVILANES"], places) is None
