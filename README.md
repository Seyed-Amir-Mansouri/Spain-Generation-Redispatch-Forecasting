# A Machine Learning Tool for Generation Redispatch Forecasting in the Spanish Electricity System

This estimator predicts where in the Spanish peninsular grid REE will curtail generation because of a grid limit. For each 15-minute delivery period and each grid cell (0.5° squares by default), it gives the probability that units in that cell are redispatched downwards in the day-ahead technical-constraint process (Phase I).

Only the restriction types that describe genuine grid limits count:

- `SCB`: overload in the base case
- `SCA`: overload under contingency
- `RTD`: distribution-grid constraint

Voltage control and other reasons are excluded.

## Why it is useful

REE publishes this redispatch per unit only 90 days after delivery. The estimator answers the same question from signals that are public on the delivery day itself: day-ahead and intraday auction prices, national and regional forecasts, and REE's national constraint totals.

A prediction is meant to be issued once the third intraday auction (IDA3) has cleared on the delivery day. Set `use_ida3 = false` in `config.toml` to issue earlier, after IDA2 on the day before.

## What the code does

1. **Per-unit redispatch (labels).**
   - **Source.** `fetch-redispatch` downloads REE's daily `I90DIA` file, which gives results per programming unit 90 days after delivery.
   - **What it keeps.** It keeps sheet 03, the Phase I technical-constraint redispatch with its direction, restriction type and energy per period.
   - **Format.** Files are CSV with 96 quarter-hours per day. The parser maps each period to its local quarter of the day (0–95), including on clock-change days. Older hourly files are rejected.
   - **Storage.** Each day is stored as a small compressed table and is never downloaded twice.
2. **Unit locations.** `fetch-units` downloads REE's registries of programming units and of the physical plants behind them. Neither has coordinates, so each plant is matched by name to these public plant lists:
   - OpenStreetMap power plants
   - the Open Power System Data plant list
   - Global Energy Monitor's Spanish solar and wind farm pages on gem.wiki, which give each plant's municipality, capacity and coordinates (CC BY 4.0)
   - the official installation maps of two regions:
     - Castilla y León, from opendata.jcyl.es: «©Junta de Castilla y León», available free at www.jcyl.es
     - Aragón, from ICEARAGON (Gobierno de Aragón)

   Two fallbacks follow for units that match no plant:
   - **Municipality.** A unit whose name spells out a Spanish municipality, such as `PS FREGENAL DE LA SIERRA`, is placed there, using OpenStreetMap towns and villages that carry an INE code. Saints and landforms ("San Cristóbal", "Sierra Cabrera") and hydro plants are skipped.
   - **Substation.** Many solar and wind plants are named after the 220 or 400 kV substation they connect to, so a solar or wind unit is placed at a substation whose name its code starts with, for example `FCAPARA` at Caparacena. The placement is refused when the unit's full name contradicts the substation.

   Both fallbacks give `low` confidence. A low-confidence placement is undone when it lies more than 1.5° from every firmly placed plant of the same programming unit.

   How a match is made:
   - **Order of attempts:** exact name, then name without its group number, then close spelling, then all distinctive words present. Last, REE's own code is tried as a shortened plant name, so `FVTAGUS` matches Tagus and `AUREAFV` matches Aurea. This last step is accepted only when the capacities agree within a factor of 1.5.
   - **Names tried.** Each plant's short name, long name and REE code are tried. When a programming unit contains only one plant, the programming unit's name is tried too, because single-plant units often carry the plant's real name while the plant itself only has a code.
   - **Technology check.** A match must fit the plant's technology, so a small hydro plant cannot match a wind farm with the same place name.
   - **Capacity check.** A match is refused when the listed plant is less than a quarter of the unit's capacity.
   - **Ambiguity.** When plants with the same name lie far apart, the one whose capacity is within a factor of 1.5 is chosen. If capacity cannot decide, the match is refused.
   - **Source order.** Global Energy Monitor and the regional maps are used only for plants that OpenStreetMap and Open Power System Data cannot place. Global Energy Monitor's short names often repeat across regions.
   - **Hand-checked plants.** `reference/manual_locations.csv` overrides the matcher for large plants whose registry names are codes, and records the evidence for each choice. A row can name a plant from one of the lists or, with origin `coordinates`, give the site's latitude and longitude.
   - **Renamed units.** `reference/unit_aliases.csv` maps retired REE unit codes to the codes that replaced them, for example `ABO2` to `ABO2G`.
   - **Confidence.** Each unit gets one of three levels:
     - `high`: an exact or hand-checked match
     - `medium`: any other plant match
     - `low`: placed at a municipality or substation
3. **Attribution.**
   - **Placed redispatch.** Each unit's redispatch is split across grid cells in proportion to the capacity of its located plants. Cross-border units are dropped.
   - **Estimated redispatch.** When `estimate_unplaced` is on, the redispatch of units that could not be placed is estimated:
     - **Matching.** Each unplaced unit's quarter-hour series is correlated with every cell's placed redispatch, and with the cell's redispatch added to that of its eight neighbours. A small weight is added for cells that already hold the same technology.
     - **Placing.** The unit's energy goes to its `estimate_top_cells` best cells, one by default. Plants behind the same grid limit are redispatched in the same periods, which is why this works.
     - **Accuracy.** In a test that hid 20% of the placed units, about 54% of their energy went to the exact cell and 77% to the exact cell or a neighbour. A random cell would get under 1%.
   - **Report.** `train` reports the placed share and the estimated share separately.
4. **Labels.** A cell and 15-minute period is congested when its attributed downward redispatch reaches `min_mwh` (1.25 MWh, the same rate as 5 MWh per hour). Days with no downloaded file are left unknown instead of being counted as uncongested.
5. **Features.**
   - **National inputs.** These are quarter-hour series for the whole of Spain:
     - `fetch-market` downloads day-ahead prices plus load, wind and solar forecasts from ENTSO-E.
     - `fetch-intraday` downloads the IDA1, IDA2 and IDA3 auction prices from OMIE's daily files. ENTSO-E has no Spanish intraday-auction prices before September 2025.
     - `fetch-constraints` downloads REE's national Phase I constraint energy by restriction type, which REE publishes on the day before delivery.
   - **Regional weather.** `fetch-weather` downloads 100 m wind speed, solar irradiance and temperature forecasts from Open-Meteo. Open-Meteo has only hourly values for Spain, so they are interpolated linearly to quarter-hours:
     - **Grid.** Forecasts come on a 1° grid, and each congestion cell uses the grid square it lies in.
     - **Sources.** History comes from Open-Meteo's archive of past forecasts, and the last seven days from its live forecast service.
     - **Expected output.** Each cell's expected wind and solar output is its located capacity multiplied by a simple power curve. Its share of the national expected output tells the model where renewable generation will concentrate.
   - **Cell inputs.** These are joined with each cell's located capacity and its own congestion history.
6. **Training and prediction.**
   - **Training.** A gradient-boosted classifier is trained with the grid cell as a categorical input. The last `test_days` are held out for scoring before the final refit.
   - **Prediction.** `predict` writes every cell and 15-minute period to the ledger and prints the cells with the highest risk.
   - **Validation.** Once REE has published the per-unit files for the predicted days, `validate` scores the ledger.

## Features

| Group | Inputs |
| --- | --- |
| Calendar | quarter of the day, hour, weekday, month, weekend flag |
| Prices (ENTSO-E, OMIE) | day-ahead, IDA1, IDA2 and IDA3 prices, and each intraday price minus day-ahead |
| National forecasts (ENTSO-E) | load, wind, solar and residual load |
| Regional weather (Open-Meteo) | 100 m wind speed, solar irradiance and temperature at the cell; expected wind, solar and total renewable output of the cell's capacity; the cell's share of national expected renewable output |
| National constraint totals (REE) | downward Phase I energy from base-case overload, contingency overload, distribution grid, voltage control and other reasons; total up and down Phase I energy |
| Cell | grid cell (categorical), cell centre, located capacity by technology |
| Cell history | share of congested periods over the last 28 and 112 days and for the same quarter of the day, shifted back 91 days because the labels arrive 90 days late |

## Layout

```
README.md
pyproject.toml
config.toml                     paths, areas, label rule, feature windows, indicators, model settings
reference/manual_locations.csv  hand-checked plant locations
reference/unit_aliases.csv      retired REE unit codes and their replacements
congestion_es/
    config.py       loads config.toml into typed objects
    esios.py        throttled client for REE's e-sios API
    i90.py          downloads I90DIA files and extracts per-unit redispatch
    locations.py    unit registries, plant lists and name matching
    areas.py        grid cells, unit-to-cell shares, attribution and capacity per cell
    market.py       ENTSO-E prices and forecasts, REE's national constraint totals, merging into quarter-hour files
    omie.py         Spanish IDA1-3 prices from OMIE's daily files
    weather.py      Open-Meteo regional forecasts and wind and solar output curves
    features.py     features and target, with lags to prevent look-ahead
    pipeline.py     loads inputs, builds datasets, trains and issues predictions
    validate.py     scores the prediction ledger against published redispatch
    estimator.py    gradient-boosted classifier: holdout scoring, refit, save, load, predict
    metrics.py      Brier score, skill, ROC AUC, precision and recall
    rolling.py      trailing-window aggregates shifted by a reporting lag
    timeseries.py   resampling of tz-aware market series to local quarter-hours
    ledger.py       prediction ledger and scoring of settled predictions
    cli.py          command line entry point
tests/              unit and end-to-end tests on synthetic data
data/ models/ outputs/  local files, not tracked
```

Relative paths in `config.toml` are resolved from this folder.

## Setup

Requires Python 3.11 or newer. From this folder:

```
python -m pip install -e ".[test]"
python -m pytest
```

API keys are read from environment variables and are never written to config or data files:

| Variable | How to get it |
| --- | --- |
| `ENTSOE_API_KEY` | free account at `transparency.entsoe.eu`, Account Settings → Web API Access |
| `ESIOS_API_KEY` | token request at `esios.ree.es/en/token` |

## Commands

```
congestion-es fetch-units                     # add --skip-download to re-run matching on the files on disk
congestion-es fetch-market --start 2025-10-01 --end 2026-10-16
congestion-es fetch-intraday --start 2025-10-01 --end 2026-10-16
congestion-es fetch-constraints --start 2025-10-01 --end 2026-10-16
congestion-es fetch-weather --start 2025-10-01 --end 2026-10-16
congestion-es fetch-redispatch --start 2025-10-01 --end 2026-07-18
congestion-es train
congestion-es predict --date 2026-10-15
congestion-es validate
```

To predict a new day, first run `fetch-market`, `fetch-intraday`, `fetch-constraints` and `fetch-weather` for that day, then `predict`.

`--end` is the day after the last delivery date. `fetch-redispatch` skips days already on disk. The other fetch commands merge new values into the existing files column by column. Requests to e-sios are spaced by `request_interval_seconds`, because REE temporarily blocks clients that send rapid or repeated requests.

## Limitations

- **Proxy label.** The label shows where units were redispatched, not which line was overloaded. Units on both sides of a bottleneck are moved, and downward redispatch marks the exporting side.
- **Plant locations.** Locations come from matching names against public plant lists, so some units are only partly placed:
  - wind and solar portfolios that group many plants under one unit
  - units with coded names

  The OpenStreetMap download covers a box around Spain, so a few matches may fall in Portugal or southern France.
- **Current fleet only.** The registries and plant lists describe the fleet as it is today, so plants that closed during the training period cannot be located.
- **Late labels.** Per-unit labels arrive 90 days late, so predictions can only be scored per cell three months after delivery.
- **Weather history.** Open-Meteo's archive stitches together the first hours of successive forecast runs, so historical weather is slightly more accurate than a forecast issued the day before would have been.
- **Missing IDA1 prices.** On some days OMIE published an empty IDA1 file. Those periods carry no IDA1 price, which the model handles as missing.
- **Data period.** All inputs and labels start on 1 October 2025, when the Spanish day-ahead market moved to 15-minute periods (`[data] start` in `config.toml`). Every source is used at that resolution except weather, which is interpolated. Earlier hourly data is not used.
