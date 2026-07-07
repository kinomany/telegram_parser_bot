from __future__ import annotations

import asyncio
import os
import socket
import traceback

from aiogram import Bot

from config import (
    DIGEST_SUBSCRIPTION_DEBUG,
    DIGEST_SUBSCRIPTION_DUE_LIMIT,
    DIGEST_SUBSCRIPTION_LOCK_MINUTES,
    DIGEST_SUBSCRIPTION_RELEASE_LOCKS_ON_STARTUP,
    DIGEST_SUBSCRIPTION_SCHEDULER_INTERVAL_SECONDS,
    DIGEST_SUBSCRIPTION_STALE_RUN_MINUTES,
    DIGEST_SUBSCRIPTION_STARTUP_RECOVERY,
)
from app.db.database import claim_due_digest_subscriptions, recover_digest_scheduler_after_startup
from app.reports.digest_subscription_service import run_digest_subscription
from app.utils.error_logging import log_exception


async def digest_scheduler_loop(bot: Bot) -> None:
    """
    Worker автосводок.

    Устойчивость:
    - всё расписание хранится в БД, поэтому после выключения/включения due-подписки стартуют снова;
    - на старте снимаем lock-и прошлого процесса, если это включено в config;
    - каждую due-подписку берём через FOR UPDATE SKIP LOCKED;
    - ошибка одной подписки не валит весь scheduler.
    """
    worker_id = f"digest-worker:{socket.gethostname()}:pid:{os.getpid()}"
    interval = max(30, int(DIGEST_SUBSCRIPTION_SCHEDULER_INTERVAL_SECONDS or 60))

    if DIGEST_SUBSCRIPTION_DEBUG:
        print(f"[digest_scheduler] started worker_id={worker_id}, interval={interval}s")

    if DIGEST_SUBSCRIPTION_STARTUP_RECOVERY:
        try:
            recovery = await recover_digest_scheduler_after_startup(
                worker_id=worker_id,
                release_locks=bool(DIGEST_SUBSCRIPTION_RELEASE_LOCKS_ON_STARTUP),
                stale_run_minutes=int(DIGEST_SUBSCRIPTION_STALE_RUN_MINUTES or 60),
                lock_minutes=int(DIGEST_SUBSCRIPTION_LOCK_MINUTES or 60),
            )
            if DIGEST_SUBSCRIPTION_DEBUG:
                print(f"[digest_scheduler] startup recovery: {recovery}")
        except Exception as error:
                                                                          
            print(f"[digest_scheduler] startup recovery error: {type(error).__name__}: {error}")
            traceback.print_exc()
            await log_exception(
                "digest_scheduler.startup_recovery",
                error,
                context={"worker_id": worker_id},
            )

    while True:
        try:
            subscriptions = await claim_due_digest_subscriptions(
                limit=int(DIGEST_SUBSCRIPTION_DUE_LIMIT or 2),
                locked_by=worker_id,
                lock_minutes=int(DIGEST_SUBSCRIPTION_LOCK_MINUTES or 60),
            )

            if DIGEST_SUBSCRIPTION_DEBUG and subscriptions:
                ids = [item.get("id") for item in subscriptions]
                print(f"[digest_scheduler] due subscriptions: {ids}")

            for subscription in subscriptions:
                subscription_id = int(subscription["id"])
                if DIGEST_SUBSCRIPTION_DEBUG:
                    print(f"[digest_scheduler] run subscription_id={subscription_id}")
                result = await run_digest_subscription(
                    subscription_id=subscription_id,
                    bot=bot,
                    debug=False,
                    locked_by=worker_id,
                )
                if DIGEST_SUBSCRIPTION_DEBUG:
                    print(f"[digest_scheduler] result subscription_id={subscription_id}: {result}")

        except asyncio.CancelledError:
            if DIGEST_SUBSCRIPTION_DEBUG:
                print("[digest_scheduler] cancelled")
            raise
        except Exception as error:
            print(f"[digest_scheduler] error: {type(error).__name__}: {error}")
            traceback.print_exc()
            await log_exception(
                "digest_scheduler.loop",
                error,
                context={"worker_id": worker_id},
            )

        await asyncio.sleep(interval)
