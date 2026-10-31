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

from rest_framework.viewsets import ModelViewSet
class NestedSerializer(Serializer):
    def validate(self, data):
        requests.get('/nested-validation')
        return data
    def update(self, obj, data):
        requests.get('/forbidden-nested-update')
        return obj
class RootSerializer(Serializer):
    child = NestedSerializer()
    def update(self, obj, data):
        requests.get('/root-update')
        return obj
class ItemViewSet(ModelViewSet):
    serializer_class = RootSerializer
urlpatterns += [path('local/<int:pk>', ItemViewSet.as_view({'patch': 'partial_update'}))]
