#!/usr/bin/env bash
# Deterministic test runner for the memory governor.
#
# Why this exists: the suite inserts events into the LIVE outbox, and the live
# worker wakes on NOTIFY and can process them before the test's own assertions
# run — producing race failures (observed 2026-09-13: 2 failures, both traced
# to the worker consuming test events mid-assertion). Pausing the worker for
# the duration of the suite makes runs deterministic.
#
# Behavior: gracefully stops the worker (kill by PID — the documented safe
# method), runs pytest with the environment loaded, and ALWAYS restarts the
# worker afterwards (trap, even on failure). The API is left untouched.
# Credentials: sourced from .env by name — never printed.

set -euo pipefail
cd "$(dirname "$0")/.."

set -a
# shellcheck disable=SC1091
source .env
set +a

WORKER_PATTERN="python -m app.worker"
WORKER_PID="$(pgrep -f "$WORKER_PATTERN" | head -1 || true)"

if [ -n "${WORKER_PID}" ]; then
  echo "pausing worker (PID ${WORKER_PID})"
  kill "${WORKER_PID}"
  for _ in $(seq 1 20); do
    pgrep -f "$WORKER_PATTERN" >/dev/null 2>&1 || break
    sleep 0.5
  done
  if pgrep -f "$WORKER_PATTERN" >/dev/null 2>&1; then
    echo "ERROR: worker did not stop; aborting (worker state unchanged)"
    exit 1
  fi
  echo "worker paused"
else
  echo "worker not running (nothing to pause)"
fi

restart_worker() {
  echo "restarting worker"
  setsid .venv/bin/python -m app.worker >> /tmp/memory-governor-worker.log 2>&1 < /dev/null &
}
trap restart_worker EXIT

.venv/bin/python -m pytest -q "$@"
