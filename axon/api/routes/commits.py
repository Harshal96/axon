from __future__ import annotations

from typing import Optional
from fastapi import APIRouter, Query
from pydantic import BaseModel
from datetime import datetime

from axon.history.reader import list_commits

router = APIRouter()


class CommitSummary(BaseModel):
    sha: str
    timestamp: Optional[datetime]
    author: Optional[str]
    branch: Optional[str]
    changed_target_count: int
    breaking_api_changes: int


@router.get("/graph/commits", response_model=list[CommitSummary])
async def get_commits(
    limit: int = Query(50, le=500),
    branch: Optional[str] = Query(None),
) -> list[CommitSummary]:
    """
    Returns recently indexed commits in descending timestamp order.
    Used by the admin UI and for providing sha values to diff/time-travel ops.
    """
    records = await list_commits(limit=limit, branch=branch)
    return [
        CommitSummary(
            sha=r["sha"],
            timestamp=r.get("ts"),
            author=r.get("author"),
            branch=r.get("branch"),
            changed_target_count=len(r.get("changed_targets") or []),
            breaking_api_changes=r.get("breaking_api_changes") or 0,
        )
        for r in records
    ]