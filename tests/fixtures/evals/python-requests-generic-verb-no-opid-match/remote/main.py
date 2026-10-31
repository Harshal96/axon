from fastapi import FastAPI
app=FastAPI()
@app.get('/items', operation_id='Get')
def listing(): pass
