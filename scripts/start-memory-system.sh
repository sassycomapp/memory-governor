#!/usr/bin/env bash
# Memory system boot script — invoked from /etc/wsl.conf [boot] as:
#   su dev-p -c '/home/dev-p/memory-governor/scripts/start-memory-system.sh'
# Starts the memory governor's two processes after PostgreSQL is up.
# Idempotent: each process starts only if not already running.
set -u
cd /home/dev-p/memory-governor || exit 1
set -a; source .env; set +a

# API — skip if already listening on 8321
if ! ss -tln 2>/dev/null | grep -q ":8321"; then
  setsid nohup .venv/bin/python -m uvicorn app.main:app --host 127.0.0.1 --port 8321 \
    >> /tmp/memory-governor.log 2>&1 < /dev/null &
  echo "$(date '+%F %T') memory-governor-api started"
else
  echo "$(date '+%F %T') memory-governor-api already running"
fi

# Worker — skip if already running (pgrep never matches itself)
if ! pgrep -f "python -m app.worker" > /dev/null; then
  setsid nohup .venv/bin/python -m app.worker \
    >> /tmp/memory-governor-worker.log 2>&1 < /dev/null &
  echo "$(date '+%F %T') memory-governor-worker started"
else
  echo "$(date '+%F %T') memory-governor-worker already running"
fi
