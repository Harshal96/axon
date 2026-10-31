from django.urls import path
from django.db.models.signals import post_save
from django.db.models import Model
from django.dispatch import receiver
import requests
class Item(Model): pass
@receiver(post_save, sender=Item)
def changed(sender, instance): requests.get('/items')
def local(request):
    item=Item()
    item.save()
urlpatterns=[path('local',local)]
