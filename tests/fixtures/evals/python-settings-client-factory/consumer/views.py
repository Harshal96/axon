from fastapi import FastAPI
from django.conf import settings
from consumer.settings import GET_DIRECTORY_API, get_directory_api, dynamic_api
from consumer.retry import invoke
from helpers import get_V1Api
import directory_sdk
api = FastAPI()
upper = settings.GET_DIRECTORY_API()
local = GET_DIRECTORY_API()
lower = get_directory_api()
inline = get_V1Api(directory_sdk)
dynamic = dynamic_api(unknown_package)

@api.get("/a")
def a():
    return invoke(upper.read_item)

@api.get("/b")
def b():
    return upper.read_item()

@api.get("/c")
def c():
    return invoke(local.read_item)

@api.get("/d")
def d():
    return invoke(lower.read_item)

@api.get("/e")
def e():
    return invoke(inline.read_item)

@api.get("/f")
def f():
    return inline.read_item()

@api.get("/dynamic")
def dynamic_view():
    return dynamic.read_item()
