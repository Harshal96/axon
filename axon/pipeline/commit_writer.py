import logging

from axon.db.connection import async_session

logger = logging.getLogger(__name__)


async def write_commit_snapshot(sha: str, success: bool, duration_ms: int) -> None:
    async with async_session() as session:
        await session.run(
            """
            MERGE (c:CommitSnapshot {sha: $sha})
            SET c.id = $sha,
                c.index_success = $success,
                c.duration_ms = $duration_ms,
                c.indexed_at = datetime()
        """,
            sha=sha,
            success=success,
            duration_ms=duration_ms,
        )
        logger.info(
            f"Recorded CommitSnapshot for {sha} (success={success}, duration={duration_ms}ms)"
        )


async def is_commit_indexed(sha: str) -> bool:
    async with async_session() as session:
        res = await session.run(
            "MATCH (c:CommitSnapshot {sha: $sha}) RETURN c.index_success AS success",
            sha=sha,
        )
        rec = await res.single()
        return rec is not None and rec["success"] is True
