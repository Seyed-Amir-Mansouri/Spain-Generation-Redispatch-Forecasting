from django.urls import path

from . import views

app_name = "dashboard"
urlpatterns = [
    path("", views.index, name="index"),
    path("api/results/", views.results, name="results"),
    path("api/map/", views.outline, name="map"),
]
