"""Eval fixture: Django path converters and url() (parser input only)."""

from django.conf.urls import url
from django.urls import path
from shop import views

urlpatterns = [
    path("orders/", views.list_orders),
    path("orders/<uuid:order_id>/", views.get_order),
    url("reports/<int:year>/", views.report),
]
