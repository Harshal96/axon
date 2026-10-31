from fastapi import FastAPI
app=FastAPI()
import requests, os
@app.get('/local')
def local():
    requests.get(os.getenv('REMOTE_URL')+'/items')
    requests.get(os.getenv('UNKNOWN_URL')+'/items')
