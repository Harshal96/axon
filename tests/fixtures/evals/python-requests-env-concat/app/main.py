from fastapi import FastAPI
app=FastAPI()
import requests, os, httpx
from urllib.parse import urljoin
@app.get('/local')
def local():
    base=os.getenv('REMOTE_URL')
    requests.get(base+'/items')
    requests.get(f'{base}/items')
    requests.get('{}/items'.format(base))
    requests.get(urljoin(base,'items'))
    session=requests.Session()
    session.get(base+'/items')
    client=httpx.Client(base_url=base)
    client.get('/items')
    requests.get('https://third-party.example.test/audit')
