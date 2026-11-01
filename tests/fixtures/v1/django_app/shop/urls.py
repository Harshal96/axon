"""Fixture Django urls (parser input only)."""

from shop import views

urlpatterns = [
    path("orders/", views.list_orders),
    path("orders/<int:order_id>/", views.get_order),
]
