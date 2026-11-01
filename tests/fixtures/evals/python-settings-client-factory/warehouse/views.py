from fastapi import FastAPI
api = FastAPI()
@api.get("/stock", operation_id="read_item")
def read_item():
    return {}
