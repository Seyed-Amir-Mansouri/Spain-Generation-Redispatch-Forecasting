# A Machine Learning Tool for Generation Redispatch Forecasting in the Spanish Electricity System

This estimator predicts which generating units REE will redispatch in the day-ahead technical-constraint process (Phase I). For each 15-minute delivery period and each REE programming unit, it says whether the unit will be moved down (`Bajar`), up (`Subir`) or not at all, with the probability of each direction. The amount is not predicted.

The restriction codes that count are set per direction in `[labels.restriction_types]`. Both directions count every Phase I code found in the I90 files, so the forecast covers all day-ahead technical-constraint redispatch:

- **Downward:** overload in the base case (`SCB`) and under contingency (`SCA`), distribution-grid constraints (`RTD`), `EST` (about a quarter of the downward energy), and the smaller `CEX`, `PEV` and `COT`.
- **Upward:** `SCB`, `SCA`, `RTD`, voltage control (`COT`, `SOT`), system security (`RSI`) and the smaller `DAN` and `PCB`. Most upward energy is gas plants started for voltage control and security.

## Why it is useful

REE publishes this redispatch per unit only 90 days after delivery. The estimator answers the same question from signals that are public on the delivery day itself: day-ahead and intraday auction prices, national and regional forecasts, REE's national constraint totals, and each unit's own recent output from ENTSO-E.

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
3. **Units.** Predictions are made per REE programming unit, the units named in the I90 files. A unit is kept when it was redispatched by at least `min_mwh` in at least `min_events` (50) quarter-hours before the held-out days, in either direction. That gives 625 units. With `location_confidence = "high"`, only units whose physical units were all located by exact name or by hand are kept, so that their weather is right: 193 units. With `use_list = true`, only the units in `reference/entsoe_units.csv` are kept: the 41 of those 193 whose actual output ENTSO-E publishes per unit (26 combined cycle, 11 solar PV, 2 nuclear, 1 pumped hydro, 1 coal). Spain reports units of 100 MW and above, so almost every gas plant is covered but few solar and no wind farms. Both directions predict for the same units. Each unit has two fixed descriptors, both categorical inputs:
   - `unit_id`, a number per unit;
   - `technology_id`, its production type from REE's programming-unit registry, grouped into solar PV, wind, combined cycle, cogeneration, solar thermal, hybrid, hydro, pumped hydro, nuclear, coal and oil, gas, biomass and waste, other, and unknown for the 28 units the registry does not list.
4. **Labels.** Each direction in `directions` gets its own labels: a unit and 15-minute period counts as redispatched in that direction when its energy there reaches `min_mwh` (1.25 MWh, the same rate as 5 MWh per hour). No unit has ever been moved up and down in the same quarter-hour. Days with no downloaded file are left unknown instead of being counted as zero.
5. **Features.**
   - **National inputs.** These are quarter-hour series for the whole of Spain:
     - `fetch-market` downloads day-ahead prices plus load, wind and solar forecasts from ENTSO-E.
     - `fetch-intraday` downloads the IDA1, IDA2 and IDA3 auction prices from OMIE's daily files. ENTSO-E has no Spanish intraday-auction prices before September 2025.
     - `fetch-constraints` downloads REE's national Phase I constraint energy by restriction type, which REE publishes on the day before delivery.
   - **Area weather.** `fetch-weather` downloads 100 m wind speed, solar irradiance and cloud cover forecasts from Open-Meteo. Open-Meteo has only hourly values for Spain, so they are interpolated linearly to quarter-hours:
     - **Spots.** Forecasts are taken at the 0.1° spot (about 10 km) nearest each predicted wind, solar or hybrid unit's capacity-weighted centre: 110 spots for the 193 units.
     - **Areas.** Spain is cut into 2° squares (`area_degrees`). Each unit gets the average over the spots in its square, whatever its own technology, because congestion follows how much a whole area's wind and solar plants produce. Units that could not be located, or whose square has no spot, have no weather.
     - **Why areas.** On the 193 units with hand-checked locations, area weather gave 2% fewer wrong calls than no weather, while weather from the 1° square holding the unit, or at the unit's own spot, gave none.
     - **Sources.** History comes from Open-Meteo's archive of past forecasts, and the last seven days from its live forecast service. Requests are spaced so the weighted calls stay under `calls_per_hour`.
   - **Unit output.** `fetch-generation` downloads ENTSO-E's actual generation per unit (every 15 minutes, published within hours), one file per day in `data/raw/entsoe_units/`. Output already includes REE's redispatch, so a delivery day only gets what is out before a day-ahead prediction at noon: the whole day two days before, and the day before up to `known_until_period` (quarter 48, noon).
     - **Own output.** The unit's output at the same quarter two days before, its mean over that day, its mean on the day before until noon, and its mean in the last hour before noon.
     - **Sister units.** Units of one technology within `plant_degrees` (0.1°) form a plant, for example SAGU1–3 or PALOS1–3. REE often moves one sister instead of another, so the unit also gets the plant's output at the same quarter two days before, its share of the plant's output and its rank in the plant that day, and the plant's and the sisters' output on the day before until noon.
     - **Codes.** ENTSO-E names units by their EIC code, which REE's registry gives for each programming unit. From late December 2025 ENTSO-E sends only the plant's code; `reference/entsoe_plants.csv` maps plant codes to programming units. The 25-hour day of the October clock change is fetched in two halves, because a query takes at most 24 hours.
   - **Not used as inputs.** Plant capacity and location are not model inputs; locations only pick each unit's area and plant. Past redispatch is not an input either, because it arrives 90 days late. ENTSO-E's planned outage notices were tested and did not help: few outages are announced before noon on the day before.
6. **Training and prediction.**
   - **Two yes/no models.** Each direction has its own LightGBM classifier for the probability that the unit is redispatched that way in the quarter-hour. Their settings are in `[model]`, with the downward model's own values in `[model.Bajar]` (more trees and stronger smoothing of the unit categories, chosen by tuning on the held-out days).
   - **Up, down or nothing.** A unit-quarter is called `down` or `up` when that model's probability is the higher of the two and reaches `threshold` (0.5), and `nothing` otherwise. Both models being sure at once does not happen in practice: on the held-out days the lower of the two probabilities never exceeded 0.2.
   - **Training.** `train` picks the units, then fits both directions, or one with `--direction`. The last `test_days` (60) days with published redispatch are held out for scoring before the final refit; `--test-days` sets another number for one run, for example `--test-days 1` to score only the last day REE has published, and 0 skips scoring. To save memory, the training table is built month by month and keeps every redispatched row but only a `negative_fraction` share of the others before the held-out days. The kept rows are weighted by 1/`negative_fraction`. The held-out days are scored on all their rows. Because the units REE picks change as the grid changes, each training row's weight is also halved for every `recency_half_life_days` (60) it lies before the last training day; 0 turns this off.
   - **Report.** Each direction's held-out probability is scored (Brier skill, AUC, precision, recall). When both directions are trained together, the up/down/nothing calls on the held-out days are counted per unit-quarter: right, missed, wrong direction and false alarms for each direction, and per quarter-hour how often a direction happened, was called, and had at least one unit right.
   - **Results.** Trained on 1 October 2025 – 18 May 2026 and tested on the 60 held-out days (19 May – 17 July 2026; 5,760 quarter-hours × 41 units = 236,160 unit-quarters). Every input is available each day before delivery; past redispatch is used only as the target. Missed counts moves called the wrong way; not real counts calls of a move that did not happen. Seed 42:

     | | Happened | Spotted | Correct | Wrong | Missed | Not real |
     | --- | --- | --- | --- | --- | --- | --- |
     | Down | 5,549 | 6,508 | 4,594 (83%) | 2,869 | 955 | 1,914 |
     | Up | 54,280 | 55,148 | 39,089 (72%) | 31,250 | 15,191 | 16,059 |

     Almost all upward redispatch in the test days (54,280 of 54,788 unit-quarters among the 193 units) falls on these 41 units, mostly gas plants. Over three seeds the wrong calls were 33,877 on average, against 42,980 without the unit-output and sister inputs (−21%): the unit's own output gave −19% and the sisters a further −2%. Changing only the random seed moves the total of wrong calls by up to about 1,000.
   - **Prediction.** `predict` runs both models and writes every unit and 15-minute period to the ledger with the two probabilities and the call, and prints the units with the most up or down quarter-hours.
   - **Validation.** Once REE has published the per-unit files for the predicted days, `validate` counts the logged calls the same way, overall and over the last `window_days`, and scores each direction's probability.

## Features

| Group | Model names |
| --- | --- |
| Unit | `unit_id`, `technology_id` |
| Calendar | `quarter_hour`, `day_of_week`, `month`, `is_weekend` |
| Prices (ENTSO-E, OMIE) | `day_ahead_price`, `intraday1_price`, `intraday2_price`, `intraday3_price`, `intraday1_minus_day_ahead_price` (and 2, 3) |
| Spain forecasts (ENTSO-E) | `demand_forecast_mw`, `wind_generation_forecast_mw`, `solar_generation_forecast_mw`, `demand_minus_wind_solar_mw` |
| Spain's Phase I redispatch (REE, published the day before) | `spain_redispatch_down_overload_normal_mwh`, `_down_overload_contingency_`, `_down_distribution_`, `_down_voltage_`, `_down_other_`, `_down_total_`, and the same for `up`, plus `spain_redispatch_up_security_mwh` |
| Weather of the unit's area (Open-Meteo) | `area_wind_speed_kmh` (at 100 m), `area_solar_radiation_wm2`, `area_cloud_cover_pct` |
| Unit's recent output (ENTSO-E) | `unit_gen_2d_mw`, `unit_gen_2d_day_mw`, `unit_gen_1d_morning_mw`, `unit_gen_1d_noon_mw` |
| Sister units at the same plant (ENTSO-E) | `plant_gen_2d_mw`, `unit_share_2d`, `unit_rank_2d`, `plant_gen_1d_morning_mw`, `sisters_gen_1d_morning_mw` |

The source files keep their original column names (`da_price`, `load_forecast_mw`, the indicator keys in `[constraint_indicators]`, Open-Meteo's variable names); `features.SOURCE_NAMES` and `spain_redispatch_name` give the model names.

## Layout

```
README.md
pyproject.toml
config.toml                     paths, unit selection, label rule, indicators, model settings
reference/manual_locations.csv  hand-checked plant locations
reference/unit_aliases.csv      retired REE unit codes and their replacements
reference/entsoe_units.csv      the 41 units predicted, whose output ENTSO-E publishes
reference/entsoe_plants.csv     ENTSO-E plant codes and their programming units
congestion_es/
    config.py       loads config.toml into typed objects
    esios.py        throttled client for REE's e-sios API
    i90.py          downloads I90DIA files and extracts per-unit redispatch
    locations.py    unit registries, plant lists, name matching and technology groups
    market.py       ENTSO-E prices and forecasts, REE's national constraint totals, merging into quarter-hour files
    omie.py         Spanish IDA1-3 prices from OMIE's daily files
    weather.py      Open-Meteo forecasts at wind and solar spots, averaged over areas
    generation.py   ENTSO-E actual output per unit, and the unit and sister inputs made from it
    features.py     model inputs with readable names, and the target
    pipeline.py     picks units, loads inputs, builds datasets, trains and issues up/down/nothing calls
    validate.py     scores the prediction ledger against published redispatch
    estimator.py    LightGBM yes/no model per direction: holdout scoring, refit, save, load, predict
    metrics.py      Brier score, skill, ROC AUC, precision, recall, and up/down/nothing calls and their counts
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
congestion-es fetch-generation --start 2025-09-29 --end 2026-10-22
congestion-es fetch-redispatch --start 2025-10-01 --end 2026-07-18
congestion-es train                           # both directions; --direction Subir for one, --test-days 1 to score only the last published day
congestion-es predict --date 2026-10-15       # needs both directions' models
congestion-es validate
```

To predict a new day, first run `fetch-market`, `fetch-intraday`, `fetch-constraints` and `fetch-weather` for that day and `fetch-generation` for the two days before it, then `predict`.

`--end` is the day after the last delivery date. `fetch-redispatch` and `fetch-generation` skip days already on disk. The other fetch commands merge new values into the existing files column by column. Requests to e-sios are spaced by `request_interval_seconds`, because REE temporarily blocks clients that send rapid or repeated requests.

## Limitations

- **Proxy label.** The label shows where units were redispatched, not which line was overloaded. Units on both sides of a bottleneck are moved: downward redispatch marks the exporting side and upward redispatch the importing side.
- **Plant locations.** Locations come from matching names against public plant lists, so some units are only partly placed:
  - wind and solar portfolios that group many plants under one unit
  - units with coded names

  The OpenStreetMap download covers a box around Spain, so a few matches may fall in Portugal or southern France.
- **Current fleet only.** The registries and plant lists describe the fleet as it is today, so plants that closed during the training period cannot be located.
- **Late labels.** Per-unit labels arrive 90 days late, so predictions can only be scored per unit three months after delivery.
- **Weather history.** Open-Meteo's archive stitches together the first hours of successive forecast runs, so historical weather is slightly more accurate than a forecast issued the day before would have been.
- **Few units.** Only units whose output ENTSO-E publishes are predicted: 41 units, almost all of the upward redispatch but little of the downward, which falls mostly on solar and wind farms below 100 MW.
- **Missing output.** On a few days ENTSO-E published almost no per-unit output (6 October 2025; 1, 12 and 29 June 2026); those days have no output inputs.
- **Missing IDA1 prices.** On some days OMIE published an empty IDA1 file. Those periods carry no IDA1 price, which the model handles as missing.
- **Data period.** Inputs and labels start on 1 October 2025, when the Spanish day-ahead market moved to 15-minute periods (`[data] start` in `config.toml`), and only 15-minute data is used. Earlier hourly data (May–September 2025) was tested, as one row per hour and as hours copied into four quarters; neither lowered the wrong calls, so it is not used.
- **No summer in training.** The held-out days are in May–July, but the 15-minute training days run from October to May.
