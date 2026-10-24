"""Settings for the Spain dashboard: a read-only dashboard, so no database, sessions or logins."""
import os
import secrets
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent.parent

# the key comes from the environment; without one, a fresh key each start is fine because nothing is signed or stored
SECRET_KEY = os.environ.get("DJANGO_SECRET_KEY") or secrets.token_urlsafe(50)
DEBUG = os.environ.get("DJANGO_DEBUG") == "1"
ALLOWED_HOSTS = ["localhost", "127.0.0.1", "100.121.87.117"]

INSTALLED_APPS = ["django.contrib.staticfiles", "dashboard"]
MIDDLEWARE = [
    "django.middleware.security.SecurityMiddleware",
    "django.middleware.common.CommonMiddleware",
    "django.middleware.clickjacking.XFrameOptionsMiddleware",
]
ROOT_URLCONF = "webui_spain.urls"
TEMPLATES = [{"BACKEND": "django.template.backends.django.DjangoTemplates", "DIRS": [], "APP_DIRS": True, "OPTIONS": {}}]
WSGI_APPLICATION = "webui_spain.wsgi.application"
DATABASES = {}

LANGUAGE_CODE = "en-gb"
TIME_ZONE = "Europe/Madrid"
USE_I18N = False
USE_TZ = True

STATIC_URL = "static/"
STATIC_ROOT = BASE_DIR / "staticfiles"

# the backtest results shown on the page and the country outlines for the map
RESULTS_FILE = BASE_DIR / "data" / "results.json"
MAP_FILE = BASE_DIR / "data" / "map.json"
