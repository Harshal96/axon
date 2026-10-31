import os
import requests
from fastapi import FastAPI

app = FastAPI()

@app.get("/local")
def local():
    requests.post(f"{os.environ['REMOTE_URL']}internal/v3/items/")
