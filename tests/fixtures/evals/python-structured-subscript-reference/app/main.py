from fastapi import FastAPI
import requests
app=FastAPI()
class First:
    def run(self): requests.get('/first')
class Second:
    def run(self): requests.get('/second')
@app.get('/local')
def run(flag):
    worker = ([First()] if flag else [Second()])[0]
    worker.run()
