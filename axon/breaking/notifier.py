import logging
import httpx

from axon.config import get_settings

logger = logging.getLogger(__name__)

async def notify_breaking_changes(
    alerts: list[dict],
    commit_sha: str,
    author: str | None,
) -> int:
    """
    Sends Slack webhook notifications for breaking changes.
    Returns the number of notifications sent.
    """
    settings = get_settings()
    webhook_url = settings.slack_webhook_url
    if not webhook_url:
        logger.info("Slack webhook URL not configured. Skipping notifications.")
        return 0

    sent_count = 0
    async with httpx.AsyncClient(timeout=5.0) as client:
        for alert in alerts:
            severity = alert.get("severity")
            alert_type = alert.get("alert_type")
            node_id = alert.get("node_id")
            old_val = str(alert.get("old_value") or "")[:120]
            new_val = str(alert.get("new_value") or "")[:120]
            teams = alert.get("affected_teams", [])
            team_str = ", ".join(teams) if teams else "None"
            
            msg = (
                f"🚨 *Breaking change detected* [`{severity}`]\n"
                f"Commit: `{commit_sha[:8]}` by {author or 'Unknown'}\n"
                f"Type: {alert_type}\n"
                f"Symbol/Endpoint: `{node_id}`\n"
                f"Old: `{old_val}`\n"
                f"New: `{new_val}`\n"
                f"Affected teams: {team_str}\n"
                f"→ Run: `connectome-mcp` → `connectome_blast_radius(label=\"{node_id}\")`"
            )
            
            payload = {"text": msg}
            
            try:
                resp = await client.post(webhook_url, json=payload)
                resp.raise_for_status()
                sent_count += 1
            except Exception as e:
                logger.warning("Failed to send Slack notification for alert %s: %s", alert.get("id"), e)
                
    return sent_count
