from __future__ import annotations

import difflib
import io
import json
import math
import re
import sqlite3
import struct
import time
import unicodedata
import zipfile
import urllib.error
import urllib.parse
import urllib.request
from collections import defaultdict
from pathlib import Path

import pandas as pd

PROGRAMMING_UNITS_ARCHIVE = 82
PHYSICAL_UNITS_ARCHIVE = 81
OPSD_URL = "https://data.open-power-system-data.org/conventional_power_plants/latest/conventional_power_plants_EU.csv"
OVERPASS_URLS = (
    "https://overpass-api.de/api/interpreter",
    "https://overpass.kumi.systems/api/interpreter",
)
OVERPASS_QUERY = """
[out:json][timeout:300][bbox:35.9,-9.4,43.9,4.4];
nwr["power"="plant"];
out center tags;
"""
CYL_URL = "https://opendata.jcyl.es/ficheros/carto/industria/energia/energia_cyl_renov_instal_polig_gpkg.zip"
ARAGON_URL = "https://icearagon.aragon.es/datosdescarga/descarga.php?file=energia/renovable.zip"
ARAGON_LAYERS = {
    "V_POL_FUNCIONAMIENTO": ("wind", "PARQUE", "POTENCIA"),
    "V_POL_AUTORIZADOS": ("wind", "PARQUE", "POTENCIA"),
    "V_VALL_AAE": ("solar", "NOMBRE", "POTENCIA_I"),
    "V_VALL_AAC": ("solar", "NOMBRE", "POTENCIA_I"),
}
GEM_API = "https://www.gem.wiki/w/api.php"
GEM_CATEGORIES = {"Solar farms in Spain": "solar", "Wind farms in Spain": "wind"}
GEM_COORDINATES = re.compile(r"\|\s*([^|\n]*?Spain)[^|\n]*\n\|\s*(-?\d+\.\d+),\s*(-?\d+\.\d+)\s*\((exact|approximate)\)")
SUBSTATION_QUERY = """
[out:json][timeout:300][bbox:35.9,-9.4,43.9,4.4];
nwr["power"="substation"]["voltage"~"(^|;)(220000|400000)(;|$)"];
out center tags;
"""
PLACE_QUERY = """
[out:json][timeout:300][bbox:35.9,-9.4,43.9,4.4];
node["place"~"^(city|town|village)$"];
out body qt;
"""
SUBSTATION_WORDS ={"subestacion", "subestacio", "set", "se", "st", "ser", "electrica", "kv", "transformacion", "colectora", "red", "ree"}
CROSS_BORDER_TYPE = "Unidad instrumental internacional"

STOP_WORDS = set("""
parque eolico eolica eolicos pe p e planta solar fotovoltaica fotovoltaico fotovoltaicas fv pv psf pf pfv hsf
central centrales c cc ciclo combinado termica termosolar termoelectrica hidroelectrica hidraulica ch salto
de del la el los las y en sl sa s l slu instalacion instalaciones energia energias huerto cf ct nuclear ugh
grupo g uf up uof plant power station wind farm park
ccc cctg ctcc cct tg tv t gas vapor cicle combinat electricidad
gr bombeo generac generacion turbina turbinas vap psfv sfv cs ufi fvs
""".split())
PLACE_WORDS = set("""
fuente fuentes puerto sierra monte montes campo campos valle torre torres llano llanos cerro cerros casas
villa santa santo cortijo dehesa vega vegas loma lomas molino molinos rio rios pinar solar parque planta
""".split())
QUALIFIERS = set("""
san santa santo sant nuestra virgen sierra serra la el los las cerro puerto alto altos monte pico cabezo collado loma
embalse rio presa salto canal barranco valle pozo cortijo casa casas finca dehesa
""".split())
NOT_PLACES = {"tarifa", "union", "energia", "central", "mayor", "solaris", "solares", "fotovoltaica", "general", "comercial", "industrial"}
ROMAN = {"i": "1", "ii": "2", "iii": "3", "iv": "4", "v": "5", "vi": "6", "vii": "7", "viii": "8", "ix": "9", "x": "10"}
OPSD_SOURCE = {
    "Natural gas": "gas", "Hydro": "hydro", "Nuclear": "nuclear", "Lignite": "coal", "Hard coal": "coal",
    "Oil": "oil", "Biomass and biogas": "biomass", "Waste": "waste",
}


def fold(text) -> str:
    """Lowercase and strip accents."""
    return unicodedata.normalize("NFKD", str(text)).encode("ascii", "ignore").decode().lower().strip()


def normalize_name(text) -> str:
    """Reduce a plant name to its distinctive tokens, dropping generic words and unit codes."""
    text = re.sub(r"([a-z])(\d)", r"\1 \2", fold(text))
    text = re.sub(r"(\d)([a-z])", r"\1 \2", text)
    raw = [token for token in re.split(r"[^a-z0-9]+", text) if token]
    tokens = []
    for i, token in enumerate(raw):
        if token in ROMAN and i == len(raw) - 1 and i > 0:
            token = ROMAN[token]
        if token not in STOP_WORDS:
            tokens.append(token)
    return " ".join(tokens)


def strip_group(key: str) -> str:
    """Drop trailing unit or group numbers, so "camarasa 1" becomes "camarasa"."""
    return re.sub(r"(\s+\d+)+$", "", key).strip()


def plant_sources(technology) -> set[str] | None:
    """Map an REE production type to the plant sources it may match, or None when unknown."""
    text = fold(technology)
    if "hibrid" in text or "hib." in text:
        return {"solar", "wind", "hydro", "battery"}
    if "solar" in text:
        return {"solar"}
    if "eolic" in text:
        return {"wind"}
    if any(word in text for word in ("hidr", "bombeo", "fluyente", "embalse")):
        return {"hydro"}
    if "nuclear" in text:
        return {"nuclear"}
    if "biomasa" in text or "biogas" in text:
        return {"biomass", "biogas"}
    if any(word in text for word in ("ciclo", "gas", "vapor", "cogener", "fuel", "petroleo", "carbon", "hulla")):
        return {"gas", "oil", "coal"}
    if "residu" in text:
        return {"waste"}
    if "bateria" in text or "almacen" in text:
        return {"battery"}
    return None


# production types grouped for the model, matched in order; unmatched types are "other" and unregistered units "unknown"
TECHNOLOGY_GROUPS = (
    ("fotovoltaica", "solar_pv"), ("solar termica", "solar_thermal"), ("eolica", "wind"), ("ciclo combinado", "combined_cycle"),
    ("cogeneracion", "cogeneration"), ("hibrida", "hybrid"), ("bombeo", "pumped_hydro"), ("hidraulica", "hydro"),
    ("fluyente", "hydro"), ("embalse", "hydro"), ("nuclear", "nuclear"), ("hulla", "coal_oil"), ("petroleo", "coal_oil"),
    ("gas natural", "gas"), ("biomasa", "biomass_waste"), ("residuos", "biomass_waste"), ("biogas", "biomass_waste"),
)
TECHNOLOGIES = tuple(dict.fromkeys(name for _, name in TECHNOLOGY_GROUPS)) + ("other", "unknown")


def technology_group(technology) -> str:
    """Group an REE production type into one of TECHNOLOGIES."""
    if not isinstance(technology, str) or not technology.strip():
        return "unknown"
    text = fold(technology)
    return next((name for key, name in TECHNOLOGY_GROUPS if key in text), "other")


def _number(value) -> float:
    """Parse an REE decimal-comma capacity value."""
    try:
        return float(str(value).replace(".", "").replace(",", "."))
    except ValueError:
        return float("nan")


def _output_mw(value) -> float:
    """Parse an OpenStreetMap plant:output:electricity tag into MW."""
    match = re.match(r"\s*([\d.,]+)\s*(kw|mw|gw)", str(value).lower())
    if not match:
        return float("nan")
    return float(match.group(1).replace(",", ".")) * {"kw": 0.001, "mw": 1.0, "gw": 1000.0}[match.group(2)]


def download_registry(client, folder: Path) -> None:
    """Save REE's programming-unit and physical-unit registries as JSON files."""
    folder.mkdir(parents=True, exist_ok=True)
    for archive_id, name in ((PROGRAMMING_UNITS_ARCHIVE, "programming_units"), (PHYSICAL_UNITS_ARCHIVE, "physical_units")):
        (folder / f"{name}.json").write_text(json.dumps(client.json_archive(archive_id), ensure_ascii=False), encoding="utf-8")


def _registry_frame(path: Path) -> pd.DataFrame:
    """Read one registry JSON file into a frame with stripped text."""
    data = json.loads(path.read_text(encoding="utf-8"))
    frame = pd.DataFrame(next(iter(data.values())))
    frame.columns = [column.strip() for column in frame.columns]
    return frame.apply(lambda s: s.str.strip() if s.dtype == object else s)


def load_registry(folder: Path) -> tuple[pd.DataFrame, pd.DataFrame]:
    """Return programming units and physical units with English column names."""
    programming = _registry_frame(folder / "programming_units.json").rename(columns={
        "Código de UP": "code", "Descripción larga": "name", "Tipo de producción": "technology",
        "Tipo de UP": "unit_type", "Sujeto del Mercado": "participant", "Potencia máxima MW": "max_mw",
        "Código EIC": "eic",
    })
    physical = _registry_frame(folder / "physical_units.json").rename(columns={
        "Código de UF": "code", "Descripción corta": "short_name", "Descripción larga": "long_name",
        "Tipo de producción": "technology", "Vinculación con UP": "programming_unit", "Potencia máxima MW": "max_mw",
    })
    programming["max_mw"] = programming["max_mw"].map(_number)
    physical["max_mw"] = physical["max_mw"].map(_number)
    return programming, physical


def download_plants(folder: Path) -> None:
    """Save the OpenStreetMap power plants around Iberia and the OPSD conventional plant list."""
    folder.mkdir(parents=True, exist_ok=True)
    request = urllib.request.Request(OPSD_URL, headers={"User-Agent": "grid-congestion"})
    with urllib.request.urlopen(request, timeout=300) as response:
        (folder / "opsd_conventional_power_plants.csv").write_bytes(response.read())

    for query, name in ((OVERPASS_QUERY, "osm_power_plants.json"), (SUBSTATION_QUERY, "osm_substations.json"), (PLACE_QUERY, "osm_places.json")):
        _overpass(query, folder / name)


def _overpass(query: str, target: Path) -> None:
    """Run an Overpass query on the first server that answers and save the JSON result."""
    payload = urllib.parse.urlencode({"data": query}).encode()
    last_error = None
    for url in OVERPASS_URLS:
        request = urllib.request.Request(url, data=payload, headers={"User-Agent": "grid-congestion"})
        try:
            with urllib.request.urlopen(request, timeout=400) as response:
                target.write_bytes(response.read())
            return
        except urllib.error.HTTPError as error:
            last_error = error
    raise RuntimeError(f"All Overpass servers failed: {last_error}")


def load_substations(folder: Path) -> pd.DataFrame:
    """Return named 220 and 400 kV substations with a matching key, or an empty table when none were downloaded."""
    path = folder / "osm_substations.json"
    if not path.exists():
        return pd.DataFrame(columns=["name", "key", "lat", "lon"])
    rows = []
    for element in json.loads(path.read_text(encoding="utf-8"))["elements"]:
        name = element.get("tags", {}).get("name")
        if not name:
            continue
        center = element.get("center", {})
        words = [w for w in normalize_name(name).split() if w not in SUBSTATION_WORDS and not w.isdigit()]
        if words:
            rows.append({"name": name, "key": " ".join(words), "lat": element.get("lat", center.get("lat")), "lon": element.get("lon", center.get("lon"))})
    return pd.DataFrame(rows)


def load_aliases(path: Path) -> dict[str, str]:
    """Return the map from retired REE unit codes to the codes that replaced them."""
    if not path.exists():
        return {}
    frame = pd.read_csv(path)
    return dict(zip(frame["old_code"], frame["new_code"]))


def load_places(folder: Path) -> pd.DataFrame:
    """Return Spanish towns and villages (those with an INE code) with a folded name, or an empty table."""
    path = folder / "osm_places.json"
    if not path.exists():
        return pd.DataFrame(columns=["name", "key", "lat", "lon"])
    rows = []
    for element in json.loads(path.read_text(encoding="utf-8"))["elements"]:
        tags = element.get("tags", {})
        if "ref:ine" not in tags or not tags.get("name"):
            continue
        for name in {tags["name"], *tags["name"].split("/"), tags.get("name:es", tags["name"])}:
            key = " ".join(re.split(r"[^a-z0-9]+", fold(name))).strip()
            if len(key) >= 6 and key not in PLACE_WORDS and key not in NOT_PLACES:
                rows.append({"name": tags["name"], "key": key, "lat": element["lat"], "lon": element["lon"]})
    return pd.DataFrame(rows).drop_duplicates(["key", "lat", "lon"]).reset_index(drop=True)


def place_match(names, places: pd.DataFrame) -> int | None:
    """Place a unit at the municipality its name spells out, as in PS FREGENAL DE LA SIERRA, when that place is unique."""
    if places.empty:
        return None
    hits = set()
    for name in names:
        text = " " + " ".join(re.split(r"[^a-z0-9]+", fold(name))).strip() + " "
        words = text.split()
        if len(words) < 2:
            continue
        for index in places.index[places["key"].map(lambda key: f" {key} " in text)]:
            key = places.at[index, "key"]
            before = text[:text.index(f" {key} ")].split()
            while before and before[-1] in {"de", "del"}:
                before = before[:-1]
            # skip saints and landforms such as "san cristobal" or "pozo de canada"
            if before and before[-1] in QUALIFIERS or key.split()[0] in QUALIFIERS:
                continue
            hits.add(index)
        last = words[-1]
        # REE cuts names at 30 characters, so the last word may be a truncated place name
        if len(name.strip()) >= 29 and len(last) >= 6 and last not in PLACE_WORDS:
            hits |= set(places.index[places["key"].str.split().str[0].str.startswith(last) & (places["key"].str.len() > len(last))])
    if not hits:
        return None
    keys = places.loc[list(hits), "key"]
    longest = [i for i in hits if not any(keys[i] != keys[j] and keys[i] in keys[j] for j in hits)]
    lats, lons = places.loc[longest, "lat"], places.loc[longest, "lon"]
    if lats.max() - lats.min() > 0.3 or lons.max() - lons.min() > 0.3:
        return None
    return longest[0]


def code_stems(name) -> set[str]:
    """Return the plant-name stems an REE code may hide, dropping one technology prefix and one suffix at most."""
    token = fold(name).replace(" ", "")
    if not re.fullmatch(r"[a-z]+\d?", token):
        return set()
    base = re.sub(r"\d$", "", token)
    heads = {base} | {base[len(h):] for h in ("fv", "pe", "ps", "f", "e", "t", "s") if base.startswith(h)}
    return heads | {s[:-len(t)] for s in heads for t in ("fv", "pv", "pe", "sol") if s.endswith(t)}


def substation_match(names, substations: pd.DataFrame) -> int | None:
    """Place a plant at the substation it is named after, as in FCAPARA for Caparacena, when no name contradicts it."""
    if substations.empty:
        return None
    firsts = substations["key"].str.split().str[0]
    described = [normalize_name(n).split() for n in names if " " in fold(n).strip()]
    for name in names:
        for stem in sorted((s for s in code_stems(name) if len(s) >= 6 and s not in PLACE_WORDS), key=len, reverse=True):
            hits = substations.index[firsts.str.startswith(stem)]
            if not len(hits):
                continue
            lats, lons = substations.loc[hits, "lat"], substations.loc[hits, "lon"]
            if lats.max() - lats.min() > 0.3 or lons.max() - lons.min() > 0.3:
                return None
            first = firsts[hits[0]]
            if any(words and not (first.startswith(words[0]) or words[0].startswith(first)) for words in described):
                return None
            return hits[0]
    return None


def _gem_call(params: dict, sleep=time.sleep) -> dict:
    """Query the GEM wiki API, retrying transient failures with growing waits."""
    url = GEM_API + "?" + urllib.parse.urlencode(params)
    for attempt in range(5):
        try:
            with urllib.request.urlopen(urllib.request.Request(url, headers={"User-Agent": "grid-congestion"}), timeout=120) as response:
                return json.loads(response.read())
        except (urllib.error.URLError, TimeoutError, ConnectionError, json.JSONDecodeError):
            if attempt == 4:
                raise
            sleep(20 * (attempt + 1))
    return {}


def parse_gem_page(title: str, text: str, source: str) -> list[dict]:
    """Extract name, aliases, capacity and coordinates from one GEM wiki plant page."""
    text = re.sub(r"<ref[^>]*/>", "", text)
    text = re.sub(r"<ref[^>]*>.*?</ref>", "", text, flags=re.S)
    name = re.sub(r"\s+(solar|wind) (farm|project|power station|plant|thermal power station)$", "", title, flags=re.I)
    alias = re.search(r"'''[^']+'''\s*\(([^)]+)\)", text)
    capacities = [float(m.group(1).replace(",", "")) for m in re.finditer(r"([\d,.]+)\s*MW", text.split("==Location==")[0])]
    return [{
        "name": name, "aliases": "; ".join(a.strip() for a in alias.group(1).split(",")) if alias else "",
        "source": source, "mw": max(capacities) if capacities else None, "location": m.group(1).strip(),
        "lat": float(m.group(2)), "lon": float(m.group(3)), "precision": m.group(4),
    } for m in GEM_COORDINATES.finditer(text)]


def download_gem(folder: Path, interval_seconds: float = 3.0, call=_gem_call, sleep=time.sleep) -> None:
    """Save Global Energy Monitor's Spanish solar and wind plant pages as one table of named coordinates."""
    rows = []
    for category, source in GEM_CATEGORIES.items():
        params = {
            "action": "query", "format": "json", "formatversion": 2, "generator": "categorymembers",
            "gcmtitle": "Category:" + category, "gcmlimit": 50, "prop": "revisions", "rvprop": "content", "rvslots": "main",
        }
        while True:
            data = call(params)
            for page in data.get("query", {}).get("pages", []):
                if "revisions" in page:
                    rows.extend(parse_gem_page(page["title"], page["revisions"][0]["slots"]["main"]["content"], source))
            sleep(interval_seconds)
            if "continue" not in data:
                break
            params.update(data["continue"])
    frame = pd.DataFrame(rows).drop_duplicates(["name", "lat", "lon"])
    frame.to_csv(folder / "gem_spain.csv", index=False)


def utm30_to_lonlat(x: float, y: float) -> tuple[float, float]:
    """Convert ETRS89 UTM zone 30N coordinates (EPSG:25830) to longitude and latitude in degrees."""
    a, f, k0 = 6378137.0, 1 / 298.257222101, 0.9996
    e2 = f * (2 - f)
    ep2 = e2 / (1 - e2)
    m = y / k0
    mu = m / (a * (1 - e2 / 4 - 3 * e2 ** 2 / 64 - 5 * e2 ** 3 / 256))
    e1 = (1 - math.sqrt(1 - e2)) / (1 + math.sqrt(1 - e2))
    phi = (mu + (3 * e1 / 2 - 27 * e1 ** 3 / 32) * math.sin(2 * mu) + (21 * e1 ** 2 / 16 - 55 * e1 ** 4 / 32) * math.sin(4 * mu)
           + (151 * e1 ** 3 / 96) * math.sin(6 * mu) + (1097 * e1 ** 4 / 512) * math.sin(8 * mu))
    n = a / math.sqrt(1 - e2 * math.sin(phi) ** 2)
    t = math.tan(phi) ** 2
    c = ep2 * math.cos(phi) ** 2
    r = a * (1 - e2) / (1 - e2 * math.sin(phi) ** 2) ** 1.5
    d = (x - 500000.0) / (n * k0)
    lat = phi - (n * math.tan(phi) / r) * (d ** 2 / 2 - (5 + 3 * t + 10 * c - 4 * c ** 2 - 9 * ep2) * d ** 4 / 24
                                           + (61 + 90 * t + 298 * c + 45 * t ** 2 - 252 * ep2 - 3 * c ** 2) * d ** 6 / 720)
    lon = (d - (1 + 2 * t + c) * d ** 3 / 6 + (5 - 2 * c + 28 * t - 3 * c ** 2 + 8 * ep2 + 24 * t ** 2) * d ** 5 / 120) / math.cos(phi)
    return math.degrees(lon) - 3.0, math.degrees(lat)


def _gpkg_centre(blob: bytes) -> tuple[float, float] | None:
    """Return the envelope centre of a GeoPackage geometry, or the point itself when it has no envelope."""
    flags = blob[3]
    order = "<" if flags & 1 else ">"
    envelope = (flags >> 1) & 7
    if envelope:
        minx, maxx, miny, maxy = struct.unpack(order + "4d", blob[8:40])
        return (minx + maxx) / 2, (miny + maxy) / 2
    wkb = blob[8:]
    wkb_order = "<" if wkb[0] == 1 else ">"
    if struct.unpack(wkb_order + "I", wkb[1:5])[0] % 1000 == 1:
        return struct.unpack(wkb_order + "2d", wkb[5:21])
    return None


def download_cyl(folder: Path) -> None:
    """Save Castilla y León's official solar and wind installation areas (©Junta de Castilla y León) with their centres."""
    request = urllib.request.Request(CYL_URL, headers={"User-Agent": "grid-congestion"})
    with urllib.request.urlopen(request, timeout=300) as response:
        archive = zipfile.ZipFile(io.BytesIO(response.read()))
    member = next(name for name in archive.namelist() if name.endswith(".gpkg"))
    path = folder / "cyl_installations.gpkg"
    path.write_bytes(archive.read(member))
    connection = sqlite3.connect(path)
    table = connection.execute("select table_name from gpkg_contents where data_type = 'features'").fetchone()[0]
    rows = []
    for name, owner, kind, kw, blob in connection.execute(f'select n_inst_ener, n_titul_inst, n_tip_inst, v_kw_tot, geom from "{table}"'):
        centre = _gpkg_centre(blob) if blob else None
        if centre is None or not name:
            continue
        lon, lat = utm30_to_lonlat(*centre)
        rows.append({"name": name, "owner": owner, "source": "wind" if "lica" in fold(kind) else "solar",
                     "mw": (kw or 0) / 1000 or None, "lat": lat, "lon": lon})
    connection.close()
    path.unlink()
    pd.DataFrame(rows).to_csv(folder / "regional_cyl.csv", index=False)


def _dbf_rows(data: bytes) -> list[dict]:
    """Read the records of a dBase table, decoding text as UTF-8 and falling back to Latin-1."""
    count, header, length = struct.unpack("<IHH", data[4:12])
    fields, offset = [], 32
    while data[offset] != 0x0D:
        fields.append((data[offset:offset + 11].split(b"\0")[0].decode("ascii"), data[offset + 16]))
        offset += 32
    rows = []
    for index in range(count):
        record, position, row = data[header + index * length + 1:header + (index + 1) * length], 0, {}
        for name, size in fields:
            raw = record[position:position + size].strip()
            try:
                row[name] = raw.decode("utf-8")
            except UnicodeDecodeError:
                row[name] = raw.decode("latin-1")
            position += size
        rows.append(row)
    return rows


def _shp_centres(data: bytes) -> list[tuple[float, float] | None]:
    """Return the bounding-box centre of each record in a shapefile, or None for empty shapes."""
    centres, offset = [], 100
    while offset < len(data):
        content = struct.unpack(">i", data[offset + 4:offset + 8])[0] * 2
        kind = struct.unpack("<i", data[offset + 8:offset + 12])[0]
        if kind == 1:
            centres.append(struct.unpack("<2d", data[offset + 12:offset + 28]))
        elif kind:
            minx, miny, maxx, maxy = struct.unpack("<4d", data[offset + 12:offset + 44])
            centres.append(((minx + maxx) / 2, (miny + maxy) / 2))
        else:
            centres.append(None)
        offset += 8 + content
    return centres


def download_aragon(folder: Path) -> None:
    """Save Aragón's official wind farm and solar plant outlines (Gobierno de Aragón, ICEARAGON) with their centres."""
    request = urllib.request.Request(ARAGON_URL, headers={"User-Agent": "grid-congestion"})
    with urllib.request.urlopen(request, timeout=600) as response:
        archive = zipfile.ZipFile(io.BytesIO(response.read()))
    members = {Path(name).name: name for name in archive.namelist()}
    rows = []
    for layer, (source, name_field, mw_field) in ARAGON_LAYERS.items():
        if f"{layer}.shp" not in members:
            continue
        records = _dbf_rows(archive.read(members[f"{layer}.dbf"]))
        for record, centre in zip(records, _shp_centres(archive.read(members[f"{layer}.shp"]))):
            if centre is None or not record.get(name_field):
                continue
            lon, lat = utm30_to_lonlat(*centre)
            mw = _number(record.get(mw_field, "").replace(".", ",")) or None
            rows.append({"name": record[name_field], "source": source, "mw": mw, "lat": lat, "lon": lon, "layer": layer})
    pd.DataFrame(rows).to_csv(folder / "regional_aragon.csv", index=False)


def load_reference(folder: Path) -> pd.DataFrame:
    """Combine OPSD and OpenStreetMap plants into one table of names, sources, capacities and coordinates."""
    opsd = pd.read_csv(folder / "opsd_conventional_power_plants.csv")
    opsd = opsd[(opsd["country"] == "ES") & opsd["lat"].notna()]
    opsd = pd.DataFrame({
        "name": opsd["name"], "lat": opsd["lat"], "lon": opsd["lon"], "origin": "opsd",
        "source": opsd["energy_source"].map(OPSD_SOURCE).fillna("other"), "ref_mw": opsd["capacity"],
    })
    elements = json.loads((folder / "osm_power_plants.json").read_text(encoding="utf-8"))["elements"]
    rows = []
    for element in elements:
        tags = element.get("tags", {})
        if not tags.get("name"):
            continue
        center = element.get("center", {})
        rows.append({
            "name": tags["name"],
            "lat": element.get("lat", center.get("lat")),
            "lon": element.get("lon", center.get("lon")),
            "origin": "osm",
            "source": tags.get("plant:source", "?"),
            "ref_mw": _output_mw(tags.get("plant:output:electricity")),
        })
    frames = [opsd, pd.DataFrame(rows)]
    gem_path = folder / "gem_spain.csv"
    if gem_path.exists():
        gem = pd.read_csv(gem_path)
        gem_rows = []
        for plant in gem.itertuples(index=False):
            for name in [plant.name, *str(plant.aliases if isinstance(plant.aliases, str) else "").split(";")]:
                if name.strip():
                    gem_rows.append({"name": name.strip(), "lat": plant.lat, "lon": plant.lon, "origin": "gem",
                                     "source": plant.source, "ref_mw": plant.mw})
        frames.append(pd.DataFrame(gem_rows))
    for path in sorted(folder.glob("regional_*.csv")):
        regional = pd.read_csv(path)
        frames.append(pd.DataFrame({"name": regional["name"], "lat": regional["lat"], "lon": regional["lon"],
                                    "origin": "regional", "source": regional["source"], "ref_mw": regional["mw"]}))
    reference = pd.concat(frames, ignore_index=True)
    reference["key"] = reference["name"].map(normalize_name)
    return reference[reference["key"] != ""].reset_index(drop=True)


class Matcher:
    def __init__(self, reference: pd.DataFrame):
        self.reference = reference
        self.by_key = defaultdict(list)
        self.by_token = defaultdict(set)
        self.by_prefix = defaultdict(list)
        for idx, key in reference["key"].items():
            self.by_key[key].append(idx)
            first = key.split()[0]
            if len(first) >= 5:
                self.by_prefix[first[:5]].append(idx)
            for token in key.split():
                if len(token) >= 4 and not token.isdigit():
                    self.by_token[token].add(idx)

    def _allowed(self, idx: int, sources: set[str] | None) -> bool:
        """Return whether a reference plant's source fits the unit's technology."""
        return sources is None or self.reference.at[idx, "source"] in sources

    def _ratio(self, idx: int, mw) -> float:
        """Return the reference plant's capacity divided by the unit's, or NaN when either is unknown."""
        ref = self.reference.at[idx, "ref_mw"]
        if not mw or mw != mw or not ref or ref != ref:
            return float("nan")
        return ref / mw

    def _together(self, candidates) -> bool:
        """Return whether all candidates sit within 0.3 degrees of each other."""
        lats = self.reference.loc[candidates, "lat"]
        lons = self.reference.loc[candidates, "lon"]
        return lats.max() - lats.min() <= 0.3 and lons.max() - lons.min() <= 0.3

    def _pick(self, candidates, sources, mw=None) -> int | None:
        """Return a candidate when the technology-compatible candidates sit at one place, using capacity to break ties."""
        candidates = [i for i in candidates if self._allowed(i, sources)]
        candidates = [i for i in candidates if not 0 < self._ratio(i, mw) < 0.25]
        if not candidates:
            return None
        if self._together(candidates):
            return candidates[0]
        close = [i for i in candidates if 0.67 <= self._ratio(i, mw) <= 1.5]
        if close and self._together(close):
            return close[0]
        return None

    def _prefix(self, names, sources, mw) -> int | None:
        """Match an REE code that is a cut-down plant name, such as FVTAGUS for Tagus, when capacity agrees."""
        for name in names:
            for stem in sorted((s for s in code_stems(name) if len(s) >= 5 and s not in PLACE_WORDS), key=len, reverse=True):
                pool = [i for i in self.by_prefix.get(stem[:5], []) if self.reference.at[i, "key"].split()[0].startswith(stem)]
                pool = [i for i in pool if self._allowed(i, sources) and 0.67 <= self._ratio(i, mw) <= 1.5]
                if pool and self._together(pool):
                    return pool[0]
        return None

    def match(self, names, technology, mw=None) -> tuple[int | None, str | None]:
        """Try exact, group-stripped, fuzzy, word-subset and code-prefix matches in that order."""
        sources = plant_sources(technology)
        keys = [key for key in dict.fromkeys(normalize_name(n) for n in names) if key]
        for key in keys:
            hit = self._pick(self.by_key.get(key, []), sources, mw)
            if hit is not None:
                return hit, "exact"
        for key in keys:
            base = strip_group(key)
            if base and base != key:
                hit = self._pick(self.by_key.get(base, []), sources, mw)
                if hit is not None:
                    return hit, "group"
        best, best_score = None, 0.0
        for key in keys:
            tokens = [t for t in key.split() if t in self.by_token]
            if not tokens:
                continue
            rare = min(tokens, key=lambda t: len(self.by_token[t]))
            for idx in self.by_token[rare]:
                if not self._allowed(idx, sources):
                    continue
                score = difflib.SequenceMatcher(None, strip_group(key), strip_group(self.reference.at[idx, "key"])).ratio()
                if score > best_score:
                    best, best_score = idx, score
        if best is not None and best_score >= 0.88:
            name = strip_group(self.reference.at[best, "key"])
            namesakes = [i for i, key in self.reference["key"].items() if strip_group(key) == name]
            hit = self._pick(namesakes, sources, mw)
            if hit is not None:
                return hit, "fuzzy"
        for key in keys:
            words = {t for t in strip_group(key).split() if len(t) >= 3 and not t.isdigit()}
            if not words or any(t not in self.by_token for t in words):
                continue
            rare = min(words, key=lambda t: len([i for i in self.by_token[t] if self._allowed(i, sources)]))
            pool = [i for i in self.by_token[rare] if self._allowed(i, sources)]
            if len(pool) > 5:
                continue
            hit = self._pick([i for i in pool if words <= set(self.reference.at[i, "key"].split())], sources, mw)
            if hit is not None:
                return hit, "subset"
        hit = self._prefix(names, sources, mw)
        if hit is not None:
            return hit, "prefix"
        return None, None


def locate_units(
    physical: pd.DataFrame,
    reference: pd.DataFrame,
    manual: pd.DataFrame,
    programming: pd.DataFrame | None = None,
    substations: pd.DataFrame | None = None,
    places: pd.DataFrame | None = None,
) -> pd.DataFrame:
    """Return coordinates, match method and confidence for every physical unit."""
    # GEM and regional lists are only used for units the other lists cannot place
    primary = Matcher(reference[~reference["origin"].isin(["gem", "regional"])])
    fallback = Matcher(reference)
    # a single-plant programming unit often carries the plant's real name
    single = physical.groupby("programming_unit")["code"].transform("size") == 1
    unit_names = {} if programming is None else programming.set_index("code")["name"].to_dict()
    folded = reference["name"].map(fold)
    overrides = {}
    points = {}
    for row in manual.itertuples(index=False):
        if row.origin == "coordinates":
            points[(row.level, row.code)] = (row.lat, row.lon, row.reference_name, row.confidence)
            continue
        hits = reference.index[(reference["origin"] == row.origin) & (folded == fold(row.reference_name))]
        if len(hits):
            overrides[(row.level, row.code)] = (hits[0], row.confidence)

    rows = []
    for unit, alone in zip(physical.itertuples(index=False), single):
        point = points.get(("UF", unit.code)) or points.get(("UP", unit.programming_unit))
        if point:
            rows.append({
                "physical_unit": unit.code, "programming_unit": unit.programming_unit, "technology": unit.technology,
                "max_mw": unit.max_mw, "lat": point[0], "lon": point[1], "method": "manual",
                "matched_name": point[2], "confidence": point[3],
            })
            continue
        override = overrides.get(("UF", unit.code)) or overrides.get(("UP", unit.programming_unit))
        if override:
            idx, method, confidence = override[0], "manual", override[1]
        else:
            names = [unit.long_name, unit.short_name]
            if alone and unit.programming_unit in unit_names:
                names.append(unit_names[unit.programming_unit])
            names.append(unit.code)
            idx, method = primary.match(names, unit.technology, unit.max_mw)
            if idx is None:
                idx, method = fallback.match(names, unit.technology, unit.max_mw)
            confidence = None if idx is None else ("high" if method == "exact" else "medium")
            renewable = bool({"solar", "wind"} & (plant_sources(unit.technology) or set()))
            fallbacks = []
            # hydro plants are named after dams and rivers, not villages
            hydro = (plant_sources(unit.technology) or set()) == {"hydro"}
            if places is not None and not hydro:
                fallbacks.append(("municipality", places, lambda: place_match(names, places)))
            if renewable and substations is not None:
                fallbacks.append(("substation", substations, lambda: substation_match(names, substations)))
            spot = None
            for label, table, find in fallbacks if idx is None else []:
                hit = find()
                if hit is not None:
                    spot = (label, table.loc[hit])
                    break
            if spot is not None:
                rows.append({
                    "physical_unit": unit.code, "programming_unit": unit.programming_unit, "technology": unit.technology,
                    "max_mw": unit.max_mw, "lat": spot[1]["lat"], "lon": spot[1]["lon"], "method": spot[0],
                    "matched_name": spot[1]["name"], "confidence": "low",
                })
                continue
        plant = reference.loc[idx] if idx is not None else None
        rows.append({
            "physical_unit": unit.code,
            "programming_unit": unit.programming_unit,
            "technology": unit.technology,
            "max_mw": unit.max_mw,
            "lat": None if plant is None else plant["lat"],
            "lon": None if plant is None else plant["lon"],
            "method": method,
            "matched_name": None if plant is None else plant["name"],
            "confidence": confidence,
        })
    return _drop_stray(pd.DataFrame(rows))


def _drop_stray(units: pd.DataFrame, limit: float = 1.5) -> pd.DataFrame:
    """Undo low-confidence placements far from every firmly placed plant of the same programming unit."""
    firm = units[units["confidence"].isin(["high", "medium"])]
    anchors = firm.groupby("programming_unit")[["lat", "lon"]].apply(lambda f: f.to_numpy())
    for index, unit in units[units["confidence"] == "low"].iterrows():
        points = anchors.get(unit["programming_unit"])
        if points is None or not len(points):
            continue
        nearest = min(((unit["lat"] - lat) ** 2 + (unit["lon"] - lon) ** 2) ** 0.5 for lat, lon in points)
        if nearest > limit:
            units.loc[index, ["lat", "lon", "method", "matched_name", "confidence"]] = None
    return units
