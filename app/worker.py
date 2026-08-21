import logging
import os
import time
from typing import Callable, Dict, Optional

import psycopg

from .outbox import NOTIFY_CHANNEL
from .storage import get_connection
from .retry import with_retry

logger = logging.getLogger("memory_governor.worker")

DEFAULT_POLL_INTERVAL_SECONDS = 60


def _noop_handler(event: dict) -> None:
    """
    Placeholder for Step 4.2. Real handler logic is wired in later phases
    (register_write -> Phase 7, gbrain_write -> Phase 12,
    scheduled_consolidation -> Phase 11, etc). This step proves the
    dispatch/mark-processed plumbing works end-to-end before any real
    business logic exists to plug into it.
    """
    logger.info(
        "worker: processed event_id=%s event_type=%s memory_id=%s (no-op handler)",
        event["event_id"], event["event_type"], event["memory_id"],
    )


HANDLER_REGISTRY: Dict[str, Callable[[dict], None]] = {
    "candidate_created": _noop_handler,
    "contradiction_detected": _noop_handler,
    "review_requested": _noop_handler,
    "review_resolved": _noop_handler,
    "candidate_superseded": _noop_handler,
    "expiry_due": _noop_handler,
    "scheduled_consolidation": _noop_handler,
    "register_write": _noop_handler,
    "gbrain_write": _noop_handler,
}


def process_event(event: dict) -> None:
    """
    Dispatches ONE event to its registered handler via with_retry()
    (Step 4.3) -- this is what closes Step 4.2's flagged interim gap.
    A raised handler exception is now caught, recorded (attempt_count/
    last_error), and either scheduled for a later retry or marked
    permanently_failed -- process_event() itself never raises and never
    stops a batch partway through.

    processed_at is set ONLY on genuine success. An event that failed
    (whether still retrying or now permanently_failed) is deliberately
    left with processed_at IS NULL -- it has not actually been processed;
    run_polling_pass()'s eligibility filter is what prevents a
    still-retrying event from being picked up again before its backoff
    window elapses, and permanently_failed = TRUE is what stops a
    surfaced, exhausted event from ever being picked up again at all.
    """
    handler = HANDLER_REGISTRY.get(event["event_type"])
    if handler is None:
        raise ValueError(f"No handler registered for event_type '{event['event_type']}'")

    succeeded = with_retry(event, handler)

    if succeeded:
        with get_connection() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    "UPDATE memory_events SET processed_at = now(), processed_by = %s WHERE event_id = %s",
                    ("worker", event["event_id"]),
                )
            conn.commit()


def run_polling_pass() -> int:
    """
    Processes all events that are BOTH unprocessed AND currently eligible
    (Step 4.3 addition): not permanently_failed, and either never
    scheduled for a delayed retry or past its next_retry_after time.
    Oldest first. Returns the number of events ATTEMPTED (not
    necessarily succeeded -- a failed retry still counts as attempted
    this pass).
    """
    with get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT * FROM memory_events
                WHERE processed_at IS NULL
                  AND permanently_failed = FALSE
                  AND (next_retry_after IS NULL OR next_retry_after <= now())
                ORDER BY created_at ASC
                """
            )
            pending = cur.fetchall()

    for event in pending:
        process_event(event)

    return len(pending)


def listen_loop(
    stop_after_seconds: Optional[float] = None,
    stop_after_iterations: Optional[int] = None,
    poll_interval_seconds: float = DEFAULT_POLL_INTERVAL_SECONDS,
) -> int:
    """
    The persistent LISTEN loop (Architecture §4). Holds ONE dedicated,
    autocommit connection open for its whole run -- LISTEN only works on a
    connection kept open across calls, unlike storage.get_connection()'s
    request-scoped, open-per-call pattern used everywhere else.

    Every wake -- real NOTIFY or the poll_interval_seconds timeout expiring
    with nothing received -- runs run_polling_pass(). A missed NOTIFY is
    therefore a latency problem, never a correctness problem: the next
    timeout tick catches whatever a dropped notification would have caught.
    The same tick is also what re-checks events whose backoff window has
    since elapsed (Step 4.3) -- no separate retry-scheduling mechanism
    exists; the normal polling cadence IS the backoff mechanism.

    stop_after_seconds/stop_after_iterations exist ONLY to make this
    testable -- a real deployment passes neither and the loop runs forever.

    Returns the number of iterations actually run.
    """
    database_url = os.environ["MEMORY_GOVERNOR_DATABASE_URL"]
    conn = psycopg.connect(database_url, autocommit=True)
    conn.execute(f"LISTEN {NOTIFY_CHANNEL}")

    start = time.monotonic()
    iterations = 0
    try:
        while True:
            if stop_after_seconds is not None and (time.monotonic() - start) >= stop_after_seconds:
                break
            if stop_after_iterations is not None and iterations >= stop_after_iterations:
                break

            for _notify in conn.notifies(timeout=poll_interval_seconds, stop_after=1):
                logger.info("worker: woke on NOTIFY")

            run_polling_pass()
            iterations += 1
    finally:
        conn.close()

    return iterations
