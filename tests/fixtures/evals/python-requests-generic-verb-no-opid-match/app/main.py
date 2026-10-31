import requests
from fastapi import FastAPI
app=FastAPI()
@app.get('/local')
def run(url): requests.get(url)
