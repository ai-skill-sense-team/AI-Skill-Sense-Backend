import asyncio
import logging
from datetime import datetime, timedelta, timezone

from agents.main_agent import ping_agent_service  # <-- 1. Import the ping helper
from database.connection import SessionLocal as UserSessionLocal
from database.models import UserQuest
from datapool.connection import SessionLocal as DataPoolSessionLocal
from datapool.models import Quest

logger = logging.getLogger(__name__)

# Check for overdue quests every 5 minutes.
EXPIRY_CHECK_INTERVAL_SECONDS = 300
# giving user 2 hours grace because whynot abhishek
GRACE_PERIOD_MS = 2 * 60 * 60 * 1000


def expire_overdue_quests() -> int:
    """
    Mark overdue active quests as failed.

    Quest durations are read from the UserQuest snapshot first, falling back to DataPool.
    User quest progress is updated in the user database.
    """
    user_db = UserSessionLocal()
    datapool_db = DataPoolSessionLocal()

    try:
        active_quests = (
            user_db.query(UserQuest)
            .filter(UserQuest.status == "active")
            .all()
        )

        if not active_quests:
            return 0

        quest_ids = {user_quest.quest_id for user_quest in active_quests}

        duration_rows = (
            datapool_db.query(Quest.quest_id, Quest.duration_ms)
            .filter(Quest.quest_id.in_(quest_ids))
            .all()
        )

        durations = {
            quest_id: duration_ms
            for quest_id, duration_ms in duration_rows
        }

        now = datetime.now(timezone.utc)
        expired_count = 0

        for user_quest in active_quests:
            # Prefer the assigned duration_ms snapshot so expiry agrees with dashboard
            duration_ms = user_quest.duration_ms or durations.get(user_quest.quest_id)

            # Leave progress unchanged if its DataPool quest is missing.
            if duration_ms is None:
                continue

            started_at = user_quest.started_at

            # PostgreSQL normally returns an aware datetime for this column.
            # Handle a naive datetime defensively if one is encountered.
            if started_at.tzinfo is None:
                started_at = started_at.replace(tzinfo=timezone.utc)

            deadline = started_at + timedelta(milliseconds=duration_ms + GRACE_PERIOD_MS)

            if deadline <= now:
                user_quest.status = "failed"
                user_quest.expired_at = deadline
                expired_count += 1

        if expired_count:
            user_db.commit()
            logger.info("Marked %s overdue quests as failed", expired_count)

        return expired_count

    except Exception:
        user_db.rollback()
        logger.exception("Quest expiry check failed")
        raise

    finally:
        user_db.close()
        datapool_db.close()


async def quest_expiry_loop() -> None:
    """Periodically check for and expire overdue quests, and keep Agent service warm."""
    while True:
        try:
            await asyncio.to_thread(expire_overdue_quests)
        except Exception:
            # Keep the background service alive if a check fails.
            logger.exception("Quest expiry cycle failed; will retry later")

        # <-- 2. Ping the Agent service every 5 minutes so Render doesn't spin it down
        try:
            await asyncio.to_thread(ping_agent_service)
        except Exception:
            logger.debug("Agent keep-alive ping failed; will retry next cycle")

        await asyncio.sleep(EXPIRY_CHECK_INTERVAL_SECONDS)