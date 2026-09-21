#!/usr/bin/env bash
set -Eeuo pipefail

BASE_DIR="${JBB_STAGING_DIR:-/opt/jubianbian-staging}"
SYSTEMD_DIR="/etc/systemd/system"

chmod 0755 "$BASE_DIR/ops/deploy-staging.sh" "$BASE_DIR/ops/monitor-staging.sh"
install -m 0644 "$BASE_DIR/ops/systemd/jubianbian-staging-deploy.service" "$SYSTEMD_DIR/"
install -m 0644 "$BASE_DIR/ops/systemd/jubianbian-staging-deploy.timer" "$SYSTEMD_DIR/"
install -m 0644 "$BASE_DIR/ops/systemd/jubianbian-staging-monitor.service" "$SYSTEMD_DIR/"
install -m 0644 "$BASE_DIR/ops/systemd/jubianbian-staging-monitor.timer" "$SYSTEMD_DIR/"
systemctl daemon-reload
systemctl enable --now jubianbian-staging-deploy.timer jubianbian-staging-monitor.timer
printf '%s\n' 'staging automation installed'
