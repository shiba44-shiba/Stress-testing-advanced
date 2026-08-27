#!/usr/bin/env bash
# timed-test.sh - run a load test for a set wall-clock time and report how long
# it actually took, with a live elapsed/remaining timer.
#
# Usage:
#   ./timed-test.sh <url> <rate> <seconds> [processes] [pipeline]
#
# Examples:
#   ./timed-test.sh http://localhost:3000/ 20000 60
#   ./timed-test.sh http://localhost:3000/api/health 5000 120 8 32
#   ./timed-test.sh "http://[::1]:3000/" 10000 30
#
# It runs stress_test.py in open-loop mode at the given request rate for the
# given number of seconds, prints a running timer, and shows total elapsed
# wall-clock time at the end. Ctrl+C stops early and still times what ran.

set -u

URL="${1:-}"
RATE="${2:-10000}"
SECONDS_TO_RUN="${3:-60}"
PROCS="${4:-8}"
PIPE="${5:-32}"

if [ -z "$URL" ]; then
    echo "usage: ./timed-test.sh <url> <rate-per-sec> <seconds> [processes] [pipeline]"
    echo "example: ./timed-test.sh http://localhost:3000/ 20000 60 8 32"
    exit 1
fi

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"

# Raise the open-file limit as high as this shell is allowed to (helps a lot).
ulimit -n 200000 2>/dev/null || true

echo "=============================================================="
echo " Timed load test"
echo "   target   : $URL"
echo "   rate     : ${RATE} req/s   duration: ${SECONDS_TO_RUN}s"
echo "   processes: ${PROCS}        pipeline: ${PIPE}"
echo "   started  : $(date '+%Y-%m-%d %H:%M:%S')"
echo "=============================================================="

START=$(date +%s)

# Background timer that prints elapsed/remaining once a second.
(
    while true; do
        NOW=$(date +%s)
        EL=$(( NOW - START ))
        RM=$(( SECONDS_TO_RUN - EL ))
        [ "$RM" -lt 0 ] && RM=0
        printf "\r  timer: %3ds elapsed | %3ds remaining   " "$EL" "$RM"
        [ "$EL" -ge "$SECONDS_TO_RUN" ] && break
        sleep 1
    done
) &
TIMER_PID=$!

# Make sure the timer dies with us.
trap 'kill "$TIMER_PID" 2>/dev/null' EXIT

python3 "$SCRIPT_DIR/stress_test.py" "$URL" --i-own-this \
    --rate "$RATE" --duration "$SECONDS_TO_RUN" \
    --processes "$PROCS" --pipeline "$PIPE" \
    --max-inflight 60000 --quiet \
    --csv "timed-$(date +%Y%m%d-%H%M%S).csv"

kill "$TIMER_PID" 2>/dev/null
END=$(date +%s)
echo
echo "=============================================================="
echo " Finished at $(date '+%Y-%m-%d %H:%M:%S')"
echo " Total wall-clock time: $(( END - START ))s"
echo " (CSV time-series saved next to this script)"
echo "=============================================================="
