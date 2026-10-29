from fastapi import APIRouter, BackgroundTasks

from axon.pipeline.coordinator import run_pipeline_async
from axon.pipeline.event_schema import PushEvent

router = APIRouter()


@router.post("/ingest/push")
async def handle_push(event: PushEvent, background_tasks: BackgroundTasks):
    background_tasks.add_task(run_pipeline_async, event.sha, event.changed_files)
    return {"status": "accepted", "sha": event.sha}
