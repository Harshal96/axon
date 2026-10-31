from fastapi import FastAPI
app=FastAPI()
@app.get('/items', operation_id='list_items')
def listing(): pass
