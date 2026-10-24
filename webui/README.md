# Spain

A Django dashboard for the day-ahead redispatch forecast: which of the 41 units REE will move up or down, shown on the
rolling 1-day backtest. It has a threshold slider, direction and day pickers, the headline figures, a night map of Spain
where each unit is a light, a day-by-day chart, and a board of every unit with its hourly calls.

## Start

Double-click `app.bat`. It creates a Python environment in `%LOCALAPPDATA%\webui_spain\.venv` the first time, installs
Django, starts the server on port 9100 and opens http://localhost:9100/. On the Tailscale network the page is at
http://100.121.87.117:9100/. Close the server window to stop it.

## Files

- `webui_spain/settings.py`: settings; `ALLOWED_HOSTS` lists the addresses the page answers on.
- `dashboard/views.py`: the page, plus `/api/results/` and `/api/map/` as JSON.
- `dashboard/templates/dashboard/index.html`, `dashboard/static/dashboard/app.css`, `app.js`: the page.
- `data/results.json`: one record per unit and day (daily and hourly up and down probabilities, and the hours REE moved it).
- `data/map.json`: country outlines (Natural Earth) already projected for the map.

## New results

After a new backtest, rebuild the data from its table (needs pandas) and reload the page:

    py -3 manage.py build_results path\to\backtest.pkl

`DJANGO_SECRET_KEY` and `DJANGO_DEBUG=1` can be set in the environment; neither is needed to run.
