import logging
from datetime import datetime, timezone

from sqlalchemy import select

from app.database import SessionLocal
from app.models import OutboxEvent, OutboxStatus

logger = logging.getLogger(__name__)


def deliver_pending_events() -> int:
    """Deliver a small batch. `SKIP LOCKED` also makes future multi-worker use safe."""
    delivered = 0
    with SessionLocal.begin() as session:
        events = session.scalars(
            select(OutboxEvent)
            .where(OutboxEvent.status == OutboxStatus.PENDING)
            .order_by(OutboxEvent.created_at)
            .limit(50)
            .with_for_update(skip_locked=True)
        ).all()
        for event in events:
            event.attempts += 1
            # A real receiver must deduplicate by this stable event id. If this
            # process dies after the call and before commit, delivery is retried.
            logger.info("delivered event id=%s type=%s payload=%s", event.id, event.event_type, event.payload)
            event.status = OutboxStatus.DELIVERED
            event.published_at = datetime.now(timezone.utc)
            delivered += 1
    return delivered
