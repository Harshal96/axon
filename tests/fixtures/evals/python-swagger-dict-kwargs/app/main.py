from fastapi import FastAPI
app=FastAPI()
import generated_sdk
class Runner:
    def __init__(self, options): self.api=options['api']
    def run(self): self.api.list_items()
@app.get('/local')
def local():
    runner=Runner({'api': generated_sdk.V1Api()})
    runner.run()
