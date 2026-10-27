"""Pages and JSON feeds. Files are read on each request, so rebuilt data shows on the next reload."""
import csv
import json
import tomllib
from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

from django.conf import settings
from django.http import JsonResponse
from django.shortcuts import render
from django.views.decorators.csrf import ensure_csrf_cookie
from django.views.decorators.http import require_GET, require_POST

from . import jobs

FIRST_DAY = date(2026, 1, 1)
DEFAULT_DAY = date(2026, 7, 17)
MAX_DAYS = 14


def _load(path):
    return json.loads(path.read_text(encoding="utf-8"))


def home(request):
    return render(request, "dashboard/home.html", {"page": "home", "days": len(_load(settings.RESULTS_FILE)["days"])})


def validation(request):
    return render(request, "dashboard/validation.html", {"page": "validation", "results": _load(settings.RESULTS_FILE), "outline": _load(settings.MAP_FILE)})


def _last_day():
    """Return tomorrow in Madrid: the last delivery day with day-ahead data."""
    return datetime.now(ZoneInfo("Europe/Madrid")).date() + timedelta(days=1)


@ensure_csrf_cookie
def forecast(request):
    limits = {"first": f"{FIRST_DAY}", "last": f"{_last_day()}", "pick": [f"{DEFAULT_DAY}", f"{DEFAULT_DAY}"]}
    try:
        days = _days(request.GET)
        limits["pick"] = [f"{days[0]}", f"{days[-1]}"]
    except ValueError:
        pass
    return render(request, "dashboard/forecast.html", {"page": "forecast", "units": _load(settings.RESULTS_FILE)["units"], "limits": limits})


def sources(request):
    return render(request, "dashboard/sources.html", {"page": "sources", "now": datetime.now(ZoneInfo("Europe/Madrid")).isoformat()})


def results(request):
    return JsonResponse(_load(settings.RESULTS_FILE))


def outline(request):
    return JsonResponse(_load(settings.MAP_FILE))


def _days(values):
    """Read first and last delivery days, inclusive, and return every day between them."""
    try:
        first, last = date.fromisoformat(values.get("first", "")), date.fromisoformat(values.get("last", ""))
    except ValueError:
        raise ValueError("Pick a first and a last day.")
    if last < first:
        raise ValueError("The last day comes before the first day.")
    if (last - first).days >= MAX_DAYS:
        raise ValueError(f"Pick at most {MAX_DAYS} days.")
    if first < FIRST_DAY:
        raise ValueError(f"Pick days from {FIRST_DAY:%d %B %Y} onward.")
    if last > _last_day():
        raise ValueError(f"The last day can be tomorrow at the latest, {_last_day():%d %B %Y}.")
    return [first + timedelta(days=i) for i in range((last - first).days + 1)]


def _error(text, status=400):
    return JsonResponse({"error": text}, status=status)


@require_GET
def plan(request):
    try:
        days = _days(request.GET)
    except ValueError as error:
        return _error(str(error))
    if jobs.busy():
        return _error("A download or forecast is running. Wait for it to finish, then check again.", 409)
    try:
        code, out, err = jobs.run_now("plan", "--start", f"{days[0]}", "--end", f"{days[-1] + timedelta(days=1)}", "--json")
    except Exception as error:
        return _error(f"Could not run the pipeline: {error}", 500)
    if code != 0:
        return _error((err.strip().splitlines() or ["The data check failed."])[-1], 500)
    return JsonResponse(json.loads(out.strip().splitlines()[-1]))


@require_GET
def job(request):
    return JsonResponse(jobs.current() or {})


@require_POST
def start_job(request):
    try:
        body = json.loads(request.body or "{}")
        kind = body.get("kind")
        days = _days(body) if kind in ("download", "forecast") else []
        if kind == "forecast" and body.get("days"):
            days = sorted({date.fromisoformat(text) for text in body["days"]} & set(days))
    except (ValueError, TypeError, json.JSONDecodeError) as error:
        return _error(str(error))
    names = [f"{day}" for day in days]
    if kind == "download":
        steps = [("Download the missing data", ["fetch-days", "--start", names[0], "--end", f"{days[-1] + timedelta(days=1)}"])]
        title = "Downloading data"
    elif kind == "forecast":
        steps = [(_span(a, b), ["predict", "--date", f"{a}", "--end", f"{b + timedelta(days=1)}"]) for a, b in _runs(days)]
        title = "Running the forecast"
    elif kind == "train":
        steps = [("Retrain the downward and upward models", ["train"])]
        title = "Retraining the models"
    else:
        return _error("Unknown job.")
    if not jobs.start(kind, title, steps, names):
        return _error("Another download or forecast is running.", 409)
    return JsonResponse(jobs.current())


def _runs(days):
    """Group days into runs of consecutive days, each as its first and last day."""
    runs = []
    for day in days:
        if runs and day == runs[-1][1] + timedelta(days=1):
            runs[-1][1] = day
        else:
            runs.append([day, day])
    return runs


def _span(first, last):
    return f"Forecast {first:%a %d %b %Y}" if first == last else f"Forecast {first:%d %b} to {last:%d %b %Y} ({(last - first).days + 1} days)"


def _settings():
    with open(settings.PROJECT_DIR / "config.toml", "rb") as handle:
        return tomllib.load(handle)


@require_GET
def forecasts(request):
    """Summarise the latest forecast of each asked day from the prediction ledger, per unit and hour."""
    try:
        days = [f"{day}" for day in _days(request.GET)]
    except ValueError as error:
        return _error(str(error))
    path = settings.PROJECT_DIR / _settings()["paths"]["predictions"]
    rows = {}
    if path.exists():
        with open(path, newline="", encoding="utf-8") as handle:
            for row in csv.DictReader(handle):
                day = row["delivery_date"][:10]
                if day in days:
                    rows.setdefault(day, []).append(row)
    out = []
    for day in days:
        found = rows.get(day, [])
        if not found:
            out.append({"day": day, "issued": None, "units": []})
            continue
        issued = max(row["issued_at"] for row in found)
        units = {}
        for row in found:
            if row["issued_at"] != issued:
                continue
            unit = units.setdefault(row["unit"], {"u": row["unit"], "up": set(), "down": set(), "pu": 0.0, "pd": 0.0})
            hour = int(row["period"]) // 4 + 1
            if row["decision"] in ("up", "down"):
                unit[row["decision"]].add(hour)
            unit["pu"] = max(unit["pu"], float(row["up_probability"]))
            unit["pd"] = max(unit["pd"], float(row["down_probability"]))
        listed = [{**unit, "up": sorted(unit["up"]), "down": sorted(unit["down"]), "pu": round(unit["pu"], 3), "pd": round(unit["pd"], 3)} for unit in units.values()]
        out.append({"day": day, "issued": issued, "units": sorted(listed, key=lambda unit: unit["u"])})
    return JsonResponse({"days": out})
