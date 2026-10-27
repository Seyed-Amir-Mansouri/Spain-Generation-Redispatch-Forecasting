from django.urls import path

from . import views

app_name = "dashboard"
urlpatterns = [
    path("", views.home, name="home"),
    path("validation/", views.validation, name="validation"),
    path("forecast/", views.forecast, name="forecast"),
    path("sources/", views.sources, name="sources"),
    path("api/results/", views.results, name="results"),
    path("api/map/", views.outline, name="map"),
    path("api/plan/", views.plan, name="plan"),
    path("api/job/", views.job, name="job"),
    path("api/job/start/", views.start_job, name="start_job"),
    path("api/forecasts/", views.forecasts, name="forecasts"),
]
