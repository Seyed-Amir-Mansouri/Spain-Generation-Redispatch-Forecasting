"""Settings for the Spain dashboard: no database, sessions or logins; forecasts run the redispatch_es pipeline."""
import os
import secrets
import shutil
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent.parent

SECRET_KEY = os.environ.get("DJANGO_SECRET_KEY") or secrets.token_urlsafe(50)
DEBUG = os.environ.get("DJANGO_DEBUG") == "1"
ALLOWED_HOSTS = ["localhost", "127.0.0.1", "100.121.87.117"]

INSTALLED_APPS = ["django.contrib.staticfiles", "dashboard"]
MIDDLEWARE = [
    "django.middleware.security.SecurityMiddleware",
    "django.middleware.common.CommonMiddleware",
    "django.middleware.csrf.CsrfViewMiddleware",
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

RESULTS_FILE = BASE_DIR / "data" / "results.json"
MAP_FILE = BASE_DIR / "data" / "map.json"

PROJECT_DIR = BASE_DIR.parent
JOB_DIR = PROJECT_DIR / "data" / "webui"
if os.environ.get("PIPELINE_PYTHON"):
    PIPELINE_PYTHON = [os.environ["PIPELINE_PYTHON"]]
elif shutil.which("py"):
    PIPELINE_PYTHON = ["py", "-3"]
else:
    PIPELINE_PYTHON = ["python"]
