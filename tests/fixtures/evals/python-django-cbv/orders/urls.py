"""Eval fixture: Django class-based view behind a spec endpoint."""

from django.urls import path

from orders.views import ItemTrackingUpsertView

urlpatterns = [
    path(
        "v1/orders/<uuid:order_id>/tracking/<uuid:tracking_id>",
        ItemTrackingUpsertView.as_view(),
    ),
]
