"""Eval fixture: CBV verb method making an outbound call (parser input)."""

import requests


class ItemTrackingUpsertView:
    def put(self, request, order_id, tracking_id):
        return requests.get("https://example.com/tracking")
