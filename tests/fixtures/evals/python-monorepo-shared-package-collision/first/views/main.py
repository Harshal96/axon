from fastapi import FastAPI
from shared.decorators import guard
app=FastAPI()
@app.get('/first')
def run(): guard()
