#!/usr/bin/env bash
set -Eeuo pipefail

BASE_URL="${JBB_STAGING_URL:-http://127.0.0.1:18000}"
health="$(curl -fsS --max-time 10 "$BASE_URL/api/health")"
metrics="$(curl -fsS --max-time 10 "$BASE_URL/api/metrics")"
printf '%s [staging-monitor] health=%s metrics=%s\n' "$(date --iso-8601=seconds)" "$health" "$metrics"
