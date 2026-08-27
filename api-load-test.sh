#!/usr/bin/env bash
# api-load-test.sh - load-test a JSON API endpoint you own.
#
# Edit the variables below, make sure the host is in allowlist.txt, then run:
#   ./api-load-test.sh
#
# It uses stress_test.py's --find-capacity to ramp load on your API and report
# the request rate where it starts to slow down or error.

# ---- edit these ----
URL="https://script.ceo/api/health"     # your API endpoint (yours only)
METHOD="GET"                             # GET, POST, PUT, DELETE ...
BODY=''                                  # JSON body for POST/PUT, e.g. '{"q":"test"}'
AUTH=''                                  # bearer token, e.g. 'abc123' (leave '' if none)
START_RATE=50                            # requests/sec to start at
STEP_RATE=50                             # add this many req/s each step
STEP_SECONDS=15                          # seconds per step
SLO_P99=800                              # stop if 99th-pct latency exceeds this (ms)
SLO_ERROR_PCT=2                          # stop if error rate exceeds this (%)
PROCESSES=4                              # CPU cores to use
# --------------------

ARGS=( "$URL" --i-own-this --find-capacity
       --method "$METHOD"
       --start-rate "$START_RATE" --step-rate "$STEP_RATE"
       --step-seconds "$STEP_SECONDS"
       --slo-p99 "$SLO_P99" --slo-error-pct "$SLO_ERROR_PCT"
       --processes "$PROCESSES"
       --header "Accept: application/json" )

[ -n "$BODY" ] && ARGS+=( --body "$BODY" --header "Content-Type: application/json" )
[ -n "$AUTH" ] && ARGS+=( --header "Authorization: Bearer $AUTH" )

python3 "$(dirname "$0")/stress_test.py" "${ARGS[@]}" --csv api-results.csv
