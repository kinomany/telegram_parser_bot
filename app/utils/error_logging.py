from __future__ import annotations

import traceback
from typing import Any


async def log_exception(
    place: str,
    error: BaseException,
    *,
    user_id: int | None = None,
    telegram_id: int | None = None,
    context: dict[str, Any] | None = None,
) -> int | None:
    """Безопасно пишет exception в bot_errors. Сам логгер не должен валить основной код."""
    try:
        from app.db.database import log_bot_error

        return await log_bot_error(
            place=place,
            error_type=type(error).__name__,
            error_text=str(error),
            traceback_text="".join(traceback.format_exception(type(error), error, error.__traceback__)),
            user_id=user_id,
            telegram_id=telegram_id,
            context=context or {},
        )
    except Exception as logger_error:
        print(f"[error_logging] failed: {type(logger_error).__name__}: {logger_error}")
        return None


async def log_text_error(
    place: str,
    error_text: str,
    *,
    error_type: str | None = None,
    user_id: int | None = None,
    telegram_id: int | None = None,
    context: dict[str, Any] | None = None,
) -> int | None:
    """Безопасно пишет текстовую ошибку без exception object."""
    try:
        from app.db.database import log_bot_error

        return await log_bot_error(
            place=place,
            error_type=error_type,
            error_text=error_text,
            traceback_text=None,
            user_id=user_id,
            telegram_id=telegram_id,
            context=context or {},
        )
    except Exception as logger_error:
        print(f"[error_logging] failed: {type(logger_error).__name__}: {logger_error}")
        return None
