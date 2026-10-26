# Spain

A Django web UI for the redispatch forecast of the 41 units. The home page offers two options:

- **Validation results** (`/validation/`): the rolling backtest against REE's published redispatch, with a threshold
  slider, direction and day pickers, the headline figures, a night map of Spain where each unit is a light, a
  day-by-day chart, and a card for every unit with its hourly calls.
- **Forecast new days** (`/forecast/`): pick up to 14 delivery days, anywhere from 1 January 2026 up to tomorrow (17 July 2026 is preselected, the last day already on disk). The page lists the data those days
  need and which of it is already on disk, downloads only what is missing, and runs the forecast. The calls for each
  day are shown as cards with the hours each unit is expected to be redispatched upward or downward.

## Start

Double-click `app.bat`. It creates a Python environment in `%LOCALAPPDATA%\webui_spain\.venv` the first time, installs
Django, starts the server on port 9100 and opens http://localhost:9100/. On the Tailscale network the page is at
http://100.121.87.117:9100/. Close the server window to stop it.

The forecast page runs the `redispatch_es` package with the Python that has it installed (`py -3` by default; set
`PIPELINE_PYTHON` to the full path of another `python.exe`). `ENTSOE_API_KEY` and `ESIOS_API_KEY` must be set in the
environment the server starts from.

## Downloads

Downloaded data goes to the project's `data/raw/` folder, which the model reads. `data/raw/download_log.json` remembers
every data set and day downloaded, so nothing is fetched twice. A day is fetched again only while its source may still
be filling it in, for example the intraday auction IDA3 before it clears on the delivery day. Only one download or
forecast runs at a time, and requests to REE stay 6 seconds apart.

## Files

- `webui_spain/settings.py`: settings; `ALLOWED_HOSTS` lists the addresses the page answers on.
- `dashboard/views.py`: the pages and the JSON feeds (`/api/results/`, `/api/map/`, `/api/plan/`, `/api/job/`,
  `/api/forecasts/`).
- `dashboard/jobs.py`: runs the downloads, forecasts and retraining in the background, one at a time.
- `dashboard/templates/dashboard/`: `base.html`, `home.html`, `validation.html` and `forecast.html`.
- `dashboard/static/dashboard/`: `app.css`, `app.js` (validation) and `forecast.js`.
- `data/results.json`: one record per unit and day of the backtest (daily and hourly up and down probabilities, and the
  hours REE moved it).
- `data/map.json`: country, coast and province outlines (Natural Earth) already projected for the map.

## New results

After a new backtest, rebuild the validation data from its table (needs pandas) and reload the page:

    py -3 manage.py build_results path\to\backtest.pkl

`DJANGO_SECRET_KEY` and `DJANGO_DEBUG=1` can be set in the environment; neither is needed to run.
