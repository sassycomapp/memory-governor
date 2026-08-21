import logging
from datetime import datetime, timedelta, timezone
from typing import Callable

from .storage import get_connection

logger = logging.getLogger("memory_governor.retry")

MAX_ATTEMPTS = 5
BASE_BACKOFF_SECONDS = 2  # exponential: 2, 4, 8, 16, 32 seconds


def compute_backoff_seconds(attempt_count: int) -> int:
    """
    attempt_count is the count AFTER this failed attempt (i.e. 1 on the
    first failure). Exponential: BASE_BACKOFF_SECONDS ** attempt_count.
    """
    return BASE_BACKOFF_SECONDS ** attempt_count


def with_retry(event: dict, handler: Callable[[dict], None]) -> bool:
    """
    Wraps a single handler call with retry/backoff bookkeeping
    (Architecture §8.3). NEVER raises back to the caller -- a caller
    processing a batch (run_polling_pass) must be able to move on to the
    next event regardless of this one's outcome. Returns True if the
    handler succeeded, False otherwise (including when permanently_failed
    is set).

    On success: caller is responsible for marking processed_at, exactly
    as before this step (with_retry does not touch processed_at itself --
    single responsibility, matches process_event()'s existing split).

    On failure: increments attempt_count, records last_error, and EITHER
    schedules next_retry_after (exponential backoff) OR, once
    MAX_ATTEMPTS is reached, sets permanently_failed = TRUE AND explicitly
    clears next_retry_after back to NULL -- there is nothing left to
    schedule, and leaving a stale timestamp there would be misleading even
    though the eligibility filter's permanently_failed check makes it
    functionally harmless.
    """
    try:
        handler(event)
        return True
    except Exception as exc:
        new_attempt_count = event["attempt_count"] + 1
        error_message = f"{type(exc).__name__}: {exc}"
        logger.warning(
            "worker: event_id=%s failed (attempt %d/%d): %s",
            event["event_id"], new_attempt_count, MAX_ATTEMPTS, error_message,
        )

        if new_attempt_count >= MAX_ATTEMPTS:
            with get_connection() as conn:
                with conn.cursor() as cur:
                    cur.execute(
                        """
                        UPDATE memory_events
                        SET attempt_count = %s, last_error = %s,
                            permanently_failed = TRUE, next_retry_after = NULL
                        WHERE event_id = %s
                        """,
                        (new_attempt_count, error_message, event["event_id"]),
                    )
                conn.commit()
            logger.error(
                "worker: event_id=%s PERMANENTLY FAILED after %d attempts: %s",
                event["event_id"], new_attempt_count, error_message,
            )
        else:
            backoff = compute_backoff_seconds(new_attempt_count)
            next_retry = datetime.now(timezone.utc) + timedelta(seconds=backoff)
            with get_connection() as conn:
                with conn.cursor() as cur:
                    cur.execute(
                        """
                        UPDATE memory_events
                        SET attempt_count = %s, last_error = %s, next_retry_after = %s
                        WHERE event_id = %s
                        """,
                        (new_attempt_count, error_message, next_retry, event["event_id"]),
                    )
                conn.commit()

        return False
