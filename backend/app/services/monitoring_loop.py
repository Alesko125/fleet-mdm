"""Фонова оцінка правил моніторингу."""

from __future__ import annotations

import asyncio
import logging

from app.services.alerts import evaluate_all
from app.services.support import expire_stale

logger = logging.getLogger(__name__)

EVALUATION_INTERVAL_SECONDS = 60


async def monitoring_loop(session_factory, redis_getter, stop_event: asyncio.Event) -> None:
    """Щохвилини перевіряє всі правила для всіх пристроїв.

    Телеметрія переоцінює правила одразу під час надходження, а цей цикл
    ловить те, що не супроводжується запитом від пристрою — насамперед
    «пристрій зник із мережі».
    """
    while not stop_event.is_set():
        try:
            async with session_factory() as session:
                result = await evaluate_all(session, redis=redis_getter())
                if result["raised"] or result["resolved"]:
                    logger.info("Моніторинг", extra=result)

                # Прострочені сеанси підтримки закриваються тут же: без цього
                # «забутий» сеанс міг би висіти відкритим необмежено довго.
                closed = await expire_stale(session, redis=redis_getter())
                if closed:
                    logger.info("Закрито сеансів підтримки", extra={"count": closed})
        except Exception as exc:  # noqa: BLE001 — цикл не має падати назавжди
            logger.warning("Оцінка правил завершилася помилкою", extra={"error": str(exc)})

        try:
            await asyncio.wait_for(stop_event.wait(), timeout=EVALUATION_INTERVAL_SECONDS)
        except asyncio.TimeoutError:
            continue
