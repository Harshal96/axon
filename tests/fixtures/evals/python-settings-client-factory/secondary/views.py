from fastapi import FastAPI
from django.conf import settings
api = FastAPI()
client = settings.GET_DIRECTORY_API()

@api.get("/secondary")
def secondary():
    return client.read_item()
