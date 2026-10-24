"""The dashboard page and its two JSON feeds. Files are read on each request, so a rebuilt results.json shows on the next reload."""
import json

from django.conf import settings
from django.http import JsonResponse
from django.shortcuts import render


def _load(path):
    return json.loads(path.read_text(encoding="utf-8"))


def index(request):
    return render(request, "dashboard/index.html", {"results": _load(settings.RESULTS_FILE), "outline": _load(settings.MAP_FILE)})


def results(request):
    return JsonResponse(_load(settings.RESULTS_FILE))


def outline(request):
    return JsonResponse(_load(settings.MAP_FILE))
