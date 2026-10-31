from django.urls import path
from rest_framework.generics import CreateAPIView
from rest_framework.serializers import Serializer
import requests
class ItemSerializer(Serializer):
    def validate(self, data):
        requests.get('/items')
        return data
class ItemView(CreateAPIView):
    serializer_class=ItemSerializer
    def perform_create(self, serializer): serializer.save()
urlpatterns=[path('local', ItemView.as_view())]
