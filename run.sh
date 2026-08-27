#!/usr/bin/env bash
# Convenience launcher: ./run.sh [URL] [profile] [processes]
set -e
URL="${1:-https://script.ceo/}"
PROFILE="${2:-standard}"
PROCS="${3:-1}"
python3 "$(dirname "$0")/stress_test.py" "$URL" --i-own-this \
    --profile "$PROFILE" --processes "$PROCS" \
    --report results.json --html report.html
