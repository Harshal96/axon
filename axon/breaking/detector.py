import logging

from axon.db.connection import async_session

logger = logging.getLogger(__name__)

_FETCH_BREAKING_PROPS = """
MATCH (pv:PropertyVersion)
WHERE pv.changed_at_commit = $sha AND pv.is_breaking = true
RETURN pv.node_id AS node_id,
       pv.node_label AS node_label,
       pv.property_name AS property_name,
       pv.old_value AS old_value,
       pv.new_value AS new_value
"""

_FETCH_SYMBOL_CONSUMERS = """
MATCH (sym:Symbol {id: $node_id})
MATCH (sf:SourceFile {path: sym.file_path})
MATCH (sf)<-[:BUILDS|PRODUCES]-(root:BazelTarget)
OPTIONAL MATCH (root)<-[:DEPENDS_ON*1..3]-(consumer:BazelTarget)
WHERE consumer IS NOT NULL
RETURN DISTINCT consumer.team_owner AS team, consumer.label AS target
"""

_FETCH_API_CONSUMERS = """
MATCH (api:ApiEndpoint {id: $node_id})
MATCH (root:BazelTarget {label: api.service_label})
OPTIONAL MATCH (root)<-[:DEPENDS_ON*1..3]-(consumer:BazelTarget)
WHERE consumer IS NOT NULL
RETURN DISTINCT consumer.team_owner AS team, consumer.label AS target
"""

_FETCH_SWAGGER_CONSUMERS = """
MATCH (sw:SwaggerSchema {id: $node_id})
MATCH (root:BazelTarget {label: sw.service_label})
OPTIONAL MATCH (root)<-[:DEPENDS_ON*1..3]-(consumer:BazelTarget)
WHERE consumer IS NOT NULL
RETURN DISTINCT consumer.team_owner AS team, consumer.label AS target
"""

_MERGE_ALERT = """
MERGE (a:BreakingChangeAlert {id: $id})
ON CREATE SET
    a.alert_type = $alert_type,
    a.severity = $severity,
    a.commit_sha = $commit_sha,
    a.node_id = $node_id,
    a.node_label = $node_label,
    a.property_name = $property_name,
    a.old_value = $old_value,
    a.new_value = $new_value,
    a.affected_teams = $affected_teams,
    a.affected_targets = $affected_targets,
    a.is_acknowledged = false,
    a.created_at = datetime()
RETURN a
"""


def classify_severity(
    node_label: str, property_name: str, consumer_count: int, team_count: int
) -> tuple[str, str]:
    if node_label == "ApiEndpoint" and property_name == "schema_hash":
        if team_count > 1:
            return "CRITICAL", "API_SCHEMA_CHANGE"
        else:
            return "HIGH", "API_SCHEMA_CHANGE"

    if node_label == "SwaggerSchema" and property_name == "content_hash":
        return "HIGH", "API_SCHEMA_CHANGE"

    if node_label == "Symbol" and property_name == "signature":
        if consumer_count > 5:
            return "HIGH", "SIGNATURE_CHANGE"
        elif consumer_count >= 1:
            return "MEDIUM", "SIGNATURE_CHANGE"
        else:
            return "LOW", "SIGNATURE_CHANGE"

    if property_name == "visibility":
        return "LOW", "VISIBILITY_REDUCED"

    return "LOW", "UNKNOWN_CHANGE"


async def detect_and_write(commit_sha: str, author: str | None = None) -> list[dict]:
    """
    Scans for breaking changes in the given commit and writes BreakingChangeAlert nodes.
    Returns the written alert records.
    """
    written_alerts = []
    
    async with async_session() as session:
        # 1. Fetch breaking properties
        res = await session.run(_FETCH_BREAKING_PROPS, sha=commit_sha)
        props = await res.data()
        
        for prop in props:
            node_id = prop["node_id"]
            node_label = prop["node_label"]
            property_name = prop["property_name"]
            
            # 2. Get consumers
            consumer_query = ""
            if node_label == "Symbol":
                consumer_query = _FETCH_SYMBOL_CONSUMERS
            elif node_label == "ApiEndpoint":
                consumer_query = _FETCH_API_CONSUMERS
            elif node_label == "SwaggerSchema":
                consumer_query = _FETCH_SWAGGER_CONSUMERS
                
            affected_teams = set()
            affected_targets = set()
            
            if consumer_query:
                c_res = await session.run(consumer_query, node_id=node_id)
                c_data = await c_res.data()
                for row in c_data:
                    if row.get("team"):
                        affected_teams.add(row["team"])
                    if row.get("target"):
                        affected_targets.add(row["target"])
            
            # 3. Classify
            severity, alert_type = classify_severity(
                node_label=node_label,
                property_name=property_name,
                consumer_count=len(affected_targets),
                team_count=len(affected_teams)
            )
            
            alert_id = f"alert::{commit_sha}::{node_id}::{property_name}"
            
            # 4. Write
            alert_res = await session.run(
                _MERGE_ALERT,
                id=alert_id,
                alert_type=alert_type,
                severity=severity,
                commit_sha=commit_sha,
                node_id=node_id,
                node_label=node_label,
                property_name=property_name,
                old_value=str(prop.get("old_value") or ""),
                new_value=str(prop.get("new_value") or ""),
                affected_teams=list(affected_teams),
                affected_targets=list(affected_targets)
            )
            
            alert_rec = await alert_res.single()
            if alert_rec and alert_rec.get("a"):
                written_alerts.append(dict(alert_rec["a"]))
                
    logger.info("%d breaking change alerts written for commit %s", len(written_alerts), commit_sha)
    return written_alerts
