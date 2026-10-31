from fastapi import FastAPI
app=FastAPI()
import generated_sdk
from functools import lru_cache
@lru_cache()
def factory():
    return generated_sdk.V1Api()
api=factory()
class Runner:
    def __init__(self, api): self.api=api
    def run(self): self.api.list_items_with_http_info(async_req=True)
@app.get('/local')
def local():
    runner=Runner(api)
    runner.run()
