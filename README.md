# A Machine Learning Tool for Generation Redispatch Forecasting in the Spanish Electricity System

This estimator predicts which generating units REE will redispatch in the day-ahead technical-constraint process (Phase I), and by how much. For each 15-minute delivery period, each REE programming unit and each direction, downward (`Bajar`) and upward (`Subir`), it gives the unit's expected redispatched MWh and the probability that it reaches `min_mwh`.

The restriction codes that count are set per direction in `[labels.restriction_types]`. Both directions count every Phase I code found in the I90 files, so the forecast covers all day-ahead technical-constraint redispatch:

- **Downward:** overload in the base case (`SCB`) and under contingency (`SCA`), distribution-grid constraints (`RTD`), `EST` (about a quarter of the downward energy), and the smaller `CEX`, `PEV` and `COT`.
- **Upward:** `SCB`, `SCA`, `RTD`, voltage control (`COT`, `SOT`), system security (`RSI`) and the smaller `DAN` and `PCB`. Most upward energy is gas plants started for voltage control and security.

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
3. **Units.** Predictions are made per REE programming unit, the units named in the I90 files. A unit is kept when it was redispatched by at least `min_mwh` in at least `min_events` (50) quarter-hours before the held-out days. That leaves 559 downward and 69 upward units, which carried about 81% of the downward and all of the upward energy on the held-out days. Each kept unit has three fixed descriptors taken from those earlier days: `unit_id`, how often it was redispatched (`unit_redispatch_rate`) and its average MWh when it was (`unit_mean_mwh`). `unit_id` is a categorical input when there are at most 254 units, the limit of the boosting library, and a plain number otherwise, so the two rates are what tell the downward units apart.
4. **Labels.** Each direction in `directions` gets its own labels. The target is a unit's redispatched MWh in a 15-minute period in that direction. A unit and period also counts as redispatched when that energy reaches `min_mwh` (1.25 MWh, the same rate as 5 MWh per hour). Days with no downloaded file are left unknown instead of being counted as zero.
5. **Features.**
   - **National inputs.** These are quarter-hour series for the whole of Spain:
     - `fetch-market` downloads day-ahead prices plus load, wind and solar forecasts from ENTSO-E.
     - `fetch-intraday` downloads the IDA1, IDA2 and IDA3 auction prices from OMIE's daily files. ENTSO-E has no Spanish intraday-auction prices before September 2025.
     - `fetch-constraints` downloads REE's national Phase I constraint energy by restriction type, which REE publishes on the day before delivery.
   - **Regional weather.** `fetch-weather` downloads 100 m wind speed, solar irradiance and temperature forecasts from Open-Meteo. Open-Meteo has only hourly values for Spain, so they are interpolated linearly to quarter-hours:
     - **Grid.** Forecasts come on a 1° grid. Each unit uses the square holding the capacity-weighted centre of its located plants, or the nearest square with a forecast; units that could not be located have no weather.
     - **Sources.** History comes from Open-Meteo's archive of past forecasts, and the last seven days from its live forecast service.
   - **Not used as inputs.** Plant capacity and location are not model inputs; locations only pick each unit's weather square. Past redispatch is not an input either, apart from the two fixed unit rates, because it arrives 90 days late.
6. **Training and prediction.**
   - **Two-part model.** Each direction has two gradient-boosted models: a classifier for the probability that the unit's redispatch reaches `min_mwh`, and a Poisson regressor for the MWh when it does. The expected MWh is their product. Below `min_mwh` lies about 4% of the downward energy, which this leaves out.
   - **Training.** `train` fits both directions, or one with `--direction`. The last `test_days` are held out for scoring before the final refit. To save memory, the training table is built month by month and keeps every redispatched row but only a `negative_fraction` share of the others before the held-out days. The kept rows are weighted by 1/`negative_fraction`. The held-out days are scored on all their rows. Because the units REE picks change as the grid changes, each training row's weight is also halved for every `recency_half_life_days` (60) it lies before the last training day; 0 turns this off.
   - **Report.** The held-out days are scored for the probability (AUC, precision, recall) and for the energy: predicted over actual MWh, mean absolute error, and correlations per unit and quarter-hour, per unit and day, per unit over the whole period and for the national total. The expected MWh runs about a quarter below the actual energy, by an amount that changes from month to month; a correction factor taken from the held-out days made per-area errors larger in an earlier area-level version, so none is applied.
   - **Threshold.** A unit is flagged when its probability reaches `threshold` (0.8), which favours precision. On the held-out days the unit models gave a precision of 0.84 downward and 0.74 upward, with recall 0.30 and 0.39. They are less accurate than the earlier models that predicted 1° areas (precision 0.94 and 0.88, per-area day correlation 0.85 and 0.92), because REE's choice between units behind the same grid limit is close to random.
   - **Prediction.** `predict` writes every unit, 15-minute period and direction to the ledger with its probability, flag and expected MWh, and prints the units with the most expected MWh.
   - **Validation.** Once REE has published the per-unit files for the predicted days, `validate` scores the ledger for each direction with the same measures.

## Features

| Group | Inputs |
| --- | --- |
| Group | Model names |
| --- | --- |
| Unit | `unit_id`, `unit_redispatch_rate`, `unit_mean_mwh` |
| Calendar | `quarter_hour`, `day_of_week`, `month`, `is_weekend` |
| Prices (ENTSO-E, OMIE) | `day_ahead_price`, `intraday1_price`, `intraday2_price`, `intraday3_price`, `intraday1_minus_day_ahead_price` (and 2, 3) |
| Spain forecasts (ENTSO-E) | `demand_forecast_mw`, `wind_generation_forecast_mw`, `solar_generation_forecast_mw`, `demand_minus_wind_solar_mw` |
| Spain's Phase I redispatch (REE, published the day before) | `spain_redispatch_down_overload_normal_mwh`, `_down_overload_contingency_`, `_down_distribution_`, `_down_voltage_`, `_down_other_`, `_down_total_`, and the same for `up`, plus `spain_redispatch_up_security_mwh` |
| Weather at the unit (Open-Meteo) | `wind_speed_kmh` (at 100 m), `solar_radiation_wm2`, `temperature_c` |

The source files keep their original column names (`da_price`, `load_forecast_mw`, the indicator keys in `[constraint_indicators]`, Open-Meteo's variable names); `features.SOURCE_NAMES` and `spain_redispatch_name` give the model names.

## Layout

```
README.md
pyproject.toml
config.toml                     paths, unit selection, label rule, indicators, model settings
reference/manual_locations.csv  hand-checked plant locations
reference/unit_aliases.csv      retired REE unit codes and their replacements
congestion_es/
    config.py       loads config.toml into typed objects
    esios.py        throttled client for REE's e-sios API
    i90.py          downloads I90DIA files and extracts per-unit redispatch
    locations.py    unit registries, plant lists and name matching
    market.py       ENTSO-E prices and forecasts, REE's national constraint totals, merging into quarter-hour files
    omie.py         Spanish IDA1-3 prices from OMIE's daily files
    weather.py      Open-Meteo regional forecasts and wind and solar output curves
    features.py     model inputs with readable names, and the target
    pipeline.py     loads inputs, builds datasets, trains and issues predictions
    validate.py     scores the prediction ledger against published redispatch
    estimator.py    probability and MWh models per direction: holdout scoring, refit, save, load, predict
    metrics.py      Brier score, skill, ROC AUC, precision, recall and energy errors
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
congestion-es train                           # both directions; --direction Subir for one
congestion-es predict --date 2026-10-15
congestion-es validate
```

To predict a new day, first run `fetch-market`, `fetch-intraday`, `fetch-constraints` and `fetch-weather` for that day, then `predict`.

`--end` is the day after the last delivery date. `fetch-redispatch` skips days already on disk. The other fetch commands merge new values into the existing files column by column. Requests to e-sios are spaced by `request_interval_seconds`, because REE temporarily blocks clients that send rapid or repeated requests.

## Limitations

- **Proxy label.** The label shows where units were redispatched, not which line was overloaded. Units on both sides of a bottleneck are moved: downward redispatch marks the exporting side and upward redispatch the importing side.
- **Plant locations.** Locations come from matching names against public plant lists, so some units are only partly placed:
  - wind and solar portfolios that group many plants under one unit
  - units with coded names

  The OpenStreetMap download covers a box around Spain, so a few matches may fall in Portugal or southern France.
- **Current fleet only.** The registries and plant lists describe the fleet as it is today, so plants that closed during the training period cannot be located.
- **Late labels.** Per-unit labels arrive 90 days late, so predictions can only be scored per unit three months after delivery.
- **Weather history.** Open-Meteo's archive stitches together the first hours of successive forecast runs, so historical weather is slightly more accurate than a forecast issued the day before would have been.
- **Missing IDA1 prices.** On some days OMIE published an empty IDA1 file. Those periods carry no IDA1 price, which the model handles as missing.
- **Data period.** All inputs and labels start on 1 October 2025, when the Spanish day-ahead market moved to 15-minute periods (`[data] start` in `config.toml`). Every source is used at that resolution except weather, which is interpolated. Earlier hourly data can be added for training by setting `[data] hourly_start`. The hourly files under `hourly_*` in `[paths]` are then split into quarter-hours, with each hour's value repeated and energy values divided by four, and placed before `start`. It is not set, so the model trains on 15-minute data only. The fetch commands only write quarter-hour files, so the hourly files come from earlier runs.
