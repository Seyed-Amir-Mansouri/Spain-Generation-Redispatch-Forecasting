# Forecasting Generation Redispatch in Spain

Every day, Red Eléctrica (REE) takes the result of the day-ahead market and checks it against the physical limits of the grid. Where the market's schedule would overload a line or leave an area without enough voltage support, REE changes it: it moves some plants **down** and starts or raises others **up**. This is the Phase I technical-constraint process, and the plants it moves are paid for it.

This project predicts those decisions. For each of 41 large generating units and each 15-minute period of the day, it says whether REE will move the unit **down** (`Bajar`), **up** (`Subir`), or leave it alone, with a probability for each direction. It does not predict how many MWh will be moved.

## Why we built it

REE does publish who was redispatched, but only **90 days after delivery**. Until then, the market only sees national totals. A plant owner, a trader or an analyst who wants to know "will this unit be called tomorrow?" has nothing to go on except experience.

The decisions are not random, though. They follow the market schedule, prices, the weather, and each plant's habits, and almost all of that is public before the day starts. So the model learns from the past (every published REE decision since October 2025) and applies what it learned to the signals available on the day, long before REE's own data appears.

## Why 41 units

Spain has hundreds of programming units, so the choice of 41 needs explaining. The list was narrowed in three steps, each for a concrete reason:

1. **625 units are redispatched often enough to learn from.** A unit needs at least 50 redispatched quarter-hours in the training period. Below that, a model has too few examples to find a pattern.
2. **193 of them have a location we trust.** Weather is an input, and it is only useful at the right place. These are the units whose plants were all located by an exact name match or checked by hand.
3. **41 of those publish their own output.** ENTSO-E publishes actual output per unit, every 15 minutes, but only for units of 100 MW or more. A unit's recent output is the strongest signal we have: whether it ran yesterday morning, and whether its sister units at the same site did, says a lot about whether REE will need it again. Adding it cut wrong calls by **21%**.

These 41 units (27 combined-cycle gas plants, 10 solar PV, 2 nuclear, 1 pumped hydro and 1 coal) are not a random sample. They carry **almost all of Spain's upward redispatch**: in the test period, 54,280 of the 54,788 upward unit-quarters among the 193 well-located units fell on them. Upward redispatch is mostly gas plants started for voltage control and system security, and those plants are big enough to report their output. So by focusing on 41 units, we cover the part of the problem that matters most, and we can predict it well.

The trade-off is downward redispatch. Most of it falls on solar and wind farms below 100 MW, which ENTSO-E does not report, so this tool covers only a small part of it.

## Why it is useful for industry

Knowing a day in advance which units REE is likely to move helps several groups:

- **Gas plant operators** can see whether a unit is likely to be called up for voltage or security. That affects fuel nominations, start-up planning, staffing, and how the unit is offered in the intraday markets.
- **Solar and other generators** can see the risk of being moved down and losing output, and factor it into their sales and hedging.
- **Traders and portfolio managers** get an early view of how the final schedule will differ from the market result, unit by unit, three months before REE confirms it.
- **Maintenance planners** can place outages on days when a unit is unlikely to be needed by the grid.
- **Investors and developers** of storage, flexible demand or new plants can see where and when REE keeps redispatching units, which is where flexibility is worth the most.
- **Analysts and regulators** can study redispatch patterns as they happen instead of waiting for the 90-day delay.

The tool gives probabilities, not certainties. Each user can choose how sure a call must be: a higher threshold gives fewer but surer calls.

## How well it works

The test that counts is a rolling backtest. Each day is predicted by models trained only on the days before it, exactly as it would be used live. Over the last 14 days with published data (4–17 July 2026), averaged over three random seeds:

| Question | Right overall | Precision | Recall |
| --- | --- | --- | --- |
| Will this unit be redispatched today? (one answer per unit and day) | 91.0% | 92.8% | 93.7% |
| Will this unit be moved in this quarter-hour? (all 55,104 unit-quarters) | about 88% | | |

Precision is the share of our calls that were real moves. Recall is the share of real moves we called. Per quarter-hour (seed 42), downward calls had 87.4% precision and 85.2% recall, and upward calls 77.5% and 81.0%. When the model says a unit will stay quiet all day (probability below 0.1), it is right 98% of the time.

## How it works

1. **What REE did.** `fetch-redispatch` downloads REE's daily I90 files and keeps the Phase I redispatch per unit, direction, restriction type and quarter-hour. A unit counts as moved in a quarter-hour when at least 1.25 MWh was moved. Every Phase I restriction type is counted.
2. **Where each unit is.** REE's registries have no coordinates, so `fetch-units` matches every plant by name against OpenStreetMap, Open Power System Data, Global Energy Monitor and the regional maps of Castilla y León and Aragón. Each match is checked against the plant's technology and capacity. Large plants with coded names are placed by hand in `reference/manual_locations.csv`. Each unit gets a confidence level: high, medium or low.
3. **What was known beforehand.** Prices from the day-ahead and the three intraday auctions; Spain's demand, wind and solar forecasts; REE's national day-ahead schedule per technology and its national Phase I totals; weather forecasts for the unit's area; the unit's and its sister units' output from ENTSO-E up to noon the day before; and how often REE moved the unit at each hour last summer.
4. **The models.** Two LightGBM classifiers, one per direction, each giving the probability that a unit is moved that way in a quarter-hour. A unit-quarter is called up or down when the higher of the two probabilities reaches 0.5. Recent days count more: a training day's weight halves every 60 days back.
5. **Prediction and checking.** `predict` writes every unit and quarter-hour to a ledger with both probabilities and the call. When REE publishes the real data, `validate` scores the ledger against it.

A prediction is meant to be issued once the third intraday auction (IDA3) has cleared on the delivery day. Set `use_ida3 = false` in `config.toml` to issue it earlier, after IDA2 on the day before.

## Inputs

| Group | Model names |
| --- | --- |
| Unit | `unit_id`, `technology_id` |
| Calendar | `quarter_hour`, `day_of_week`, `month`, `is_weekend` |
| Prices (ENTSO-E, OMIE) | `day_ahead_price`, `intraday1_price`, `intraday2_price`, `intraday3_price`, `intraday1_minus_day_ahead_price` (and 2, 3) |
| Spain forecasts (ENTSO-E) | `demand_forecast_mw`, `wind_generation_forecast_mw`, `solar_generation_forecast_mw`, `demand_minus_wind_solar_mw` |
| Spain's day-ahead schedule (REE) | `spain_schedule_combined_cycle_mwh`, `_nuclear_`, `_wind_`, `_solar_pv_`, `_solar_thermal_`, `_hydro_`, `_pumped_generation_`, `_pumped_consumption_`, `_cogeneration_`, `_import_france_`, `_export_france_`, `_import_portugal_`, `_export_portugal_` |
| Spain's Phase I redispatch totals (REE) | `spain_redispatch_down_overload_normal_mwh`, `_down_overload_contingency_`, `_down_distribution_`, `_down_voltage_`, `_down_other_`, `_down_total_`, the same for `up`, and `spain_redispatch_up_security_mwh` |
| Weather of the unit's area (Open-Meteo) | `area_wind_speed_kmh`, `area_solar_radiation_wm2`, `area_cloud_cover_pct` |
| Unit's recent output (ENTSO-E) | `unit_gen_2d_mw`, `unit_gen_2d_day_mw`, `unit_gen_1d_morning_mw`, `unit_gen_1d_noon_mw` |
| Sister units at the same site (ENTSO-E) | `plant_gen_2d_mw`, `unit_share_2d`, `unit_rank_2d`, `plant_gen_1d_morning_mw`, `sisters_gen_1d_morning_mw` |
| Last summer's routine (REE hourly I90, May–September 2025) | `unit_down_rate_month_hour`, `unit_down_rate_hour`, `unit_up_rate_month_hour`, `unit_up_rate_hour` |

Past redispatch per unit is never an input, because it arrives 90 days late. It is used only to train and score the models.

## How to Run

### 1. What you need

- Windows with Python 3.11 or newer from https://www.python.org/downloads/ (tick "Add python.exe to PATH" and keep the
  `py` launcher).
- Git from https://git-scm.com/downloads, or the ZIP from GitHub's Code → Download ZIP.
- About 1 GB of free disk space.

### 2. Get the code

```
git clone https://github.com/Seyed-Amir-Mansouri/Spain-Generation-Redispatch-Forecasting.git
```

The repository holds the code, the two trained models in `models/`, and the unit files they were trained on
(`data/processed/unit_locations.csv` and REE's unit registry in `data/raw/registry/`). The market, weather, output and
redispatch data are not included; the forecast page downloads what each day needs.

### 3. API keys

Two free API keys are read from environment variables and are never written to files.

| Variable | How to get it |
| --- | --- |
| `ENTSOE_API_KEY` | Register at https://transparency.entsoe.eu/usrm/user/createPublicUser, then email transparency@entsoe.eu with the subject "Restful API access" and your account email in the body. Once access is granted, generate the token under My Account Settings → Web API Security Token. |
| `ESIOS_API_KEY` | Request a personal token at https://www.esios.ree.es/en/token. REE sends it by email. |

Set each key once as a Windows user variable, then open a new terminal or File Explorer window so it is picked up:

```
setx ENTSOE_API_KEY "your-entsoe-token"
setx ESIOS_API_KEY "your-esios-token"
```

ENTSO-E allows up to 400 requests a minute. e-sios answers 403 when called too fast, so the downloads keep requests
about 6 seconds apart and never run two e-sios jobs at once.

### 4. Start the web UI

Double-click `webui\app.bat`. The first start takes a few minutes: it creates a Python environment in
`%LOCALAPPDATA%\webui_spain\.venv` and installs Django and this package with its libraries (pandas, LightGBM and the
rest). It then starts the server on port 9100 and opens http://localhost:9100/. Close the server window to stop it.

### 5. Forecast a day

On the Forecast page, pick the delivery day, for example yesterday, and press **Check data**. The page lists what the
day needs: the market data and weather of that day, and each unit's actual output on the two days before. Press the
**Download**; only missing data is fetched and saved in `data/raw/`. Then press **Run the forecast**. The data for a day is
complete about one day after delivery: OMIE's free intraday price files come out only after the delivery day, and
ENTSO-E publishes unit output late. If the page marks a data set as late, download again later. The Data Status page
shows what is out now.

The Validation page works without any download: its results are in `webui/data/results.json`.

### 6. Command line

The same steps run from a terminal with the environment's Python:

```
set PY=%LOCALAPPDATA%\webui_spain\.venv\Scripts\python.exe
%PY% -m redispatch_es plan --start 2026-10-28
%PY% -m redispatch_es fetch-days --start 2026-10-28
%PY% -m redispatch_es predict --date 2026-10-28
```

Forecasts are added to `outputs/redispatch_decisions.csv`.

### 7. Retrain (optional)

Retraining needs the full history: run the `fetch-*` commands under Commands for the whole period, then `train`. This
takes many hours, mostly because of the gap between e-sios requests. It also needs REE's hourly redispatch files for
May to September 2025 in `data/raw/i90/`, which `fetch-redispatch` no longer reads because it expects quarter-hours.
Forecasting does not need them: the summer 2025 routine each unit had is stored inside the model files.

To run the tests, from this folder:

```
python -m pip install -e ".[test]"
python -m pytest
```

## Commands

```
redispatch-es fetch-units
redispatch-es fetch-market --start 2025-10-01 --end 2026-10-16
redispatch-es fetch-intraday --start 2025-10-01 --end 2026-10-16
redispatch-es fetch-constraints --start 2025-10-01 --end 2026-10-16
redispatch-es fetch-schedule --start 2025-10-01 --end 2026-10-16
redispatch-es fetch-weather --start 2025-10-01 --end 2026-10-16
redispatch-es fetch-generation --start 2025-09-29 --end 2026-10-22
redispatch-es fetch-redispatch --start 2025-10-01 --end 2026-07-18
redispatch-es train
redispatch-es backtest --days 14
redispatch-es plan --start 2026-10-27 --end 2026-10-29
redispatch-es fetch-days --start 2026-10-27 --end 2026-10-29
redispatch-es predict --date 2026-10-15
redispatch-es validate
```

`--end` is the day after the last delivery date. To predict a new day, `plan` lists the data it needs (market, intraday, constraint, schedule and weather data for that day, and the unit output for the two days before it) and which of it is already on disk; `fetch-days` downloads only the missing items and records them in `data/raw/download_log.json`, so nothing is downloaded twice. Then run `predict`. Requests to REE are spaced out, because REE briefly blocks clients that send rapid requests.

## Dashboard

`webui/` holds a Django web UI with two options. **Validation results** shows the backtest: headline figures, a map of Spain where each unit glows by forecast, outcome or technology, a day-by-day chart, and a card for every unit with its hourly calls. **Forecast new days** lets you pick delivery days, shows the data they need, downloads only what is missing and runs the forecast. Double-click `webui/app.bat` to start it at http://localhost:9100/. See `webui/README.md`.

## Project layout

```
config.toml          paths, unit selection, label rule, data sources and model settings
reference/           hand-checked plant locations, renamed unit codes, the 41 units and their ENTSO-E plant codes
redispatch_es/       the package: data download, unit locations, inputs, models, backtest, prediction and validation
tests/               tests on synthetic data
webui/               the dashboard
data/ models/ outputs/   local files, not tracked
```

## Limitations

- **Which units, not why.** The label shows which units REE redispatched, not which grid constraint caused it. One constraint often moves units on both sides: some down, others up.
- **Little downward coverage.** Most downward redispatch falls on solar and wind farms below 100 MW, which publish no output, so they are not among the 41 units.
- **Late scoring.** Live predictions can only be checked three months later, when REE publishes the per-unit data.
- **Short history.** Spain's market moved to 15-minute periods on 1 October 2025, and only 15-minute data is used for training. Last summer's hourly data is used only for each unit's routine.
- **Weather history.** Past weather comes from an archive of forecasts that is slightly more accurate than a real day-ahead forecast would have been.
- **Gaps.** On a few days ENTSO-E published almost no unit output, or OMIE an empty IDA1 file; those values are left missing.
