"""Retry utilities for LLM API calls with exponential backoff."""

import asyncio
import logging

from pydantic_ai.exceptions import ModelHTTPError

from hr_breaker.config import get_settings

logger = logging.getLogger(__name__)

RETRYABLE_STATUS_CODES = {429, 500, 502, 503, 504}


def is_retryable(exc: BaseException) -> bool:
    """Check if exception is retryable (rate limit or transient server error).

    Notes:
        Some upstream retry stacks can surface an internal `KeyError('idle_for')`.
        Treat it as transient so user flows don't fail immediately.
    """
    if isinstance(exc, KeyError) and exc.args == ("idle_for",):
        return True
    # Defensive fallback: some wrappers may re-raise this as generic exception text.
    if "idle_for" in str(exc):
        return True
    if isinstance(exc, ModelHTTPError):
        return exc.status_code in RETRYABLE_STATUS_CODES
    status = getattr(exc, "status_code", None)
    if isinstance(status, int):
        return status in RETRYABLE_STATUS_CODES
    return False


async def run_with_retry(
    func,
    *args,
    _max_attempts: int | None = None,
    _max_wait: float | None = None,
    **kwargs,
):
    """Run an async callable with retry on rate limits and transient errors.

    Args:
        func: Async callable to run.
        *args: Positional args passed to func.
        _max_attempts: Override max retry attempts (default: from settings).
        _max_wait: Override max wait seconds (default: from settings).
        **kwargs: Keyword args passed to func.
    """
    settings = get_settings()
    max_attempts = _max_attempts or settings.retry_max_attempts
    max_wait = _max_wait or settings.retry_max_wait

    if max_attempts < 1:
        raise ValueError("max_attempts must be >= 1")

    attempt = 1
    sleep_s = 1.0

    while True:
        try:
            return await func(*args, **kwargs)
        except Exception as exc:
            retryable = is_retryable(exc)
            # Для "idle_for" даём больше шансов, т.к. это известная transient-аномалия.
            effective_max_attempts = max_attempts + 3 if "idle_for" in str(exc) else max_attempts

            if attempt >= effective_max_attempts or not retryable:
                raise

            current_sleep = min(sleep_s, max_wait)
            logger.warning(
                "Retryable error on attempt %d/%d: %s. Retrying in %.2fs",
                attempt,
                effective_max_attempts,
                type(exc).__name__,
                current_sleep,
            )
            await asyncio.sleep(current_sleep)
            sleep_s = min(sleep_s * 2, max_wait)
            attempt += 1
