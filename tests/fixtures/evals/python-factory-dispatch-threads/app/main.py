from fastapi import FastAPI
app=FastAPI()
import generated_sdk
from threading import Thread
from concurrent.futures import ThreadPoolExecutor
class Worker:
    def run(self): generated_sdk.V1Api().list_items()
def factory(kind):
    return {1: Worker}[kind]()
@app.get('/local')
def local():
    worker=factory(1)
    Thread(target=worker.run).start()
    executor=ThreadPoolExecutor()
    executor.submit(worker.run)
