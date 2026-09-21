#!/usr/bin/env bash
set -Eeuo pipefail

REPO_DIR="${JBB_REPO_DIR:-/opt/jubianbian-github-check}"
STAGING_DIR="${JBB_STAGING_DIR:-/opt/jubianbian-staging}"
BRANCH="${JBB_DEPLOY_BRANCH:-main}"
LOCK_FILE="/run/lock/jubianbian-staging-deploy.lock"

log() {
  printf '%s [staging-deploy] %s\n' "$(date --iso-8601=seconds)" "$*"
}

mkdir -p "$(dirname "$LOCK_FILE")" "$REPO_DIR" "$STAGING_DIR"
exec 9>"$LOCK_FILE"
if ! flock -n 9; then
  log "another deployment is already running; skip"
  exit 0
fi

if [[ ! -d "$REPO_DIR/.git" ]]; then
  log "repository is missing at $REPO_DIR"
  exit 1
fi

export GIT_TERMINAL_PROMPT=0
git_without_proxy() {
  env -u HTTP_PROXY -u HTTPS_PROXY -u ALL_PROXY \
    -u http_proxy -u https_proxy -u all_proxy \
    git "$@"
}

git_without_proxy -C "$REPO_DIR" fetch --prune origin "$BRANCH"
target_commit="$(git -C "$REPO_DIR" rev-parse "origin/$BRANCH")"
deployed_commit="$(cat "$STAGING_DIR/.deployed_commit" 2>/dev/null || true)"
if [[ "$target_commit" == "$deployed_commit" ]]; then
  log "already deployed $target_commit"
  exit 0
fi

git_without_proxy -C "$REPO_DIR" reset --hard "$target_commit"

# Keep the server-only fangzhou.env, .env files, uploads, SQLite data and logs.
rsync -a \
  --exclude='.git/' \
  --exclude='*.env' \
  --exclude='data/' \
  --exclude='outputs/' \
  --exclude='node_modules/' \
  --exclude='*.tar.gz' \
  --exclude='docker-compose.yml' \
  --exclude='docker-compose.staging.yml' \
  "$REPO_DIR/" "$STAGING_DIR/"
cp -a "$REPO_DIR/docker-compose.staging.yml" "$STAGING_DIR/docker-compose.yml"

# rsync follows the Git file mode. Keep systemd entrypoints executable and
# refresh installed units so a deployment cannot break the next timer run.
chmod 0755 "$STAGING_DIR/ops/deploy-staging.sh" "$STAGING_DIR/ops/monitor-staging.sh"
install -m 0644 "$STAGING_DIR/ops/systemd/jubianbian-staging-deploy.service" \
  /etc/systemd/system/jubianbian-staging-deploy.service
install -m 0644 "$STAGING_DIR/ops/systemd/jubianbian-staging-deploy.timer" \
  /etc/systemd/system/jubianbian-staging-deploy.timer
install -m 0644 "$STAGING_DIR/ops/systemd/jubianbian-staging-monitor.service" \
  /etc/systemd/system/jubianbian-staging-monitor.service
install -m 0644 "$STAGING_DIR/ops/systemd/jubianbian-staging-monitor.timer" \
  /etc/systemd/system/jubianbian-staging-monitor.timer
systemctl daemon-reload

docker compose \
  --env-file "$STAGING_DIR/fangzhou.env" \
  -p jubianbian-staging \
  -f "$STAGING_DIR/docker-compose.yml" \
  config --quiet
docker compose \
  --env-file "$STAGING_DIR/fangzhou.env" \
  -p jubianbian-staging \
  -f "$STAGING_DIR/docker-compose.yml" \
  up -d --build

health_code=""
for attempt in $(seq 1 30); do
  health_code="$(env -u HTTP_PROXY -u HTTPS_PROXY -u ALL_PROXY \
    -u http_proxy -u https_proxy -u all_proxy \
    curl -sS --max-time 15 -o /dev/null -w '%{http_code}' \
    http://127.0.0.1:18000/api/health || true)"
  if [[ "$health_code" == "200" ]]; then
    break
  fi
  log "health check attempt $attempt/30 returned HTTP ${health_code:-000}; waiting for staging"
  sleep 2
done
if [[ "$health_code" != "200" ]]; then
  log "health check failed after 30 attempts with HTTP ${health_code:-000}"
  exit 1
fi

printf '%s\n' "$target_commit" > "$STAGING_DIR/.deployed_commit"
log "deployed $target_commit; staging health HTTP $health_code"
