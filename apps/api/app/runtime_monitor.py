"""One bounded observation at a time, independent of startup delivery recovery."""

import asyncio
import logging

from .config import Settings
from .db import SessionLocal, get_schema_readiness
from .models import utcnow
from .runtime_health import RuntimeHealthMetrics, read_runtime_snapshot

logger = logging.getLogger(__name__)
OBSERVATION_TIMEOUT_SECONDS = 2
OBSERVATION_INTERVAL_SECONDS = 10


async def monitor_runtime_health(metrics: RuntimeHealthMetrics, settings: Settings) -> None:
    try:
        while True:
            try:
                async with asyncio.timeout(OBSERVATION_TIMEOUT_SECONDS):
                    if not (await get_schema_readiness()).ready:
                        metrics.unavailable()
                    else:
                        async with SessionLocal() as session:
                            snapshot = await read_runtime_snapshot(
                                session, now=utcnow(),
                                worker_offline_seconds=settings.worker_offline_seconds,
                            )
                        # Publish only after all reads and connection cleanup succeed.
                        metrics.publish(snapshot)
            except Exception:
                metrics.unavailable()
                # DB exceptions may carry private DSNs, SQL parameters, or payloads.
                logger.warning("runtime health observation failed; retrying")
            await asyncio.sleep(OBSERVATION_INTERVAL_SECONDS)
    finally:
        metrics.unavailable()
