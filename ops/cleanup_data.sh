#!/usr/bin/env bash
set -euo pipefail

PROJECT_DIR="${JBB_PROJECT_DIR:-/opt/jubianbian}"
cd "${PROJECT_DIR}"
docker compose exec -T app python /app/ops/cleanup_data.py
