#!/usr/bin/env bash
# Convenience launcher. Edit URL/profile as needed.
set -e
URL="${1:-https://script.ceo/}"
PROFILE="${2:-standard}"
python3 "$(dirname "$0")/stress_test.py" "$URL" --i-own-this --profile "$PROFILE" --report results.json
