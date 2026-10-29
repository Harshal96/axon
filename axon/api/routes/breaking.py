from datetime import datetime
from typing import Optional

from fastapi import APIRouter, HTTPException, Query
from pydantic import BaseModel

from axon.api.cache import cached
from axon.db.connection import async_session

router = APIRouter()

class BreakingChangeAlertSummary(BaseModel):
    id: str
    alert_type: str
    severity: str
    commit_sha: str
    node_id: str
    affected_teams: list[str]
    affected_target_count: int
    is_acknowledged: bool
    created_at: datetime

class AcknowledgeRequest(BaseModel):
    acknowledged_by: str

class AcknowledgeResponse(BaseModel):
    id: str
    is_acknowledged: bool
    acknowledged_by: str
    acknowledged_at: datetime

_FETCH_ALERTS = """
MATCH (a:BreakingChangeAlert)
WHERE ($commit IS NULL OR a.commit_sha = $commit)
  AND ($severity IS NULL OR a.severity = $severity)
  AND a.is_acknowledged = $acknowledged
RETURN a
ORDER BY
  CASE a.severity WHEN 'CRITICAL' THEN 0 WHEN 'HIGH' THEN 1
                  WHEN 'MEDIUM' THEN 2 ELSE 3 END,
  a.created_at DESC
LIMIT $limit
"""

_ACK_ALERT = """
MATCH (a:BreakingChangeAlert {id: $alert_id})
SET a.is_acknowledged = true,
    a.acknowledged_by = $by,
    a.acknowledged_at = datetime()
RETURN a
"""

@router.get("/breaking/alerts", response_model=list[BreakingChangeAlertSummary])
@cached(ttl=30)
async def get_alerts(
    commit: Optional[str] = Query(None),
    severity: Optional[str] = Query(None),
    acknowledged: bool = Query(False),
    limit: int = Query(50)
):
    async with async_session() as session:
        res = await session.run(
            _FETCH_ALERTS, commit=commit, severity=severity, acknowledged=acknowledged, limit=limit
        )
        data = await res.data()
        
    out = []
    for row in data:
        a = row["a"]
        out.append(BreakingChangeAlertSummary(
            id=a["id"],
            alert_type=a["alert_type"],
            severity=a["severity"],
            commit_sha=a["commit_sha"],
            node_id=a["node_id"],
            affected_teams=a.get("affected_teams", []),
            affected_target_count=len(a.get("affected_targets", [])),
            is_acknowledged=a.get("is_acknowledged", False),
            created_at=a["created_at"].to_native() if hasattr(a["created_at"], "to_native") else a["created_at"]
        ))
    return out

@router.post("/breaking/alerts/{alert_id}/acknowledge", response_model=AcknowledgeResponse)
async def acknowledge_alert(alert_id: str, req: AcknowledgeRequest):
    async with async_session() as session:
        res = await session.run(_ACK_ALERT, alert_id=alert_id, by=req.acknowledged_by)
        rec = await res.single()
        if not rec:
            raise HTTPException(status_code=404, detail="Alert not found")
            
        a = rec["a"]
        return AcknowledgeResponse(
            id=a["id"],
            is_acknowledged=a["is_acknowledged"],
            acknowledged_by=a["acknowledged_by"],
            acknowledged_at=a["acknowledged_at"].to_native() if hasattr(a["acknowledged_at"], "to_native") else a["acknowledged_at"]
        )
