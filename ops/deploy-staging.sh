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
git -C "$REPO_DIR" fetch --prune origin "$BRANCH"
target_commit="$(git -C "$REPO_DIR" rev-parse "origin/$BRANCH")"
deployed_commit="$(cat "$STAGING_DIR/.deployed_commit" 2>/dev/null || true)"
if [[ "$target_commit" == "$deployed_commit" ]]; then
  log "already deployed $target_commit"
  exit 0
fi

git -C "$REPO_DIR" reset --hard "$target_commit"

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

health_code="$(curl -sS --max-time 15 -o /dev/null -w '%{http_code}' http://127.0.0.1:18000/api/health)"
if [[ "$health_code" != "200" ]]; then
  log "health check failed with HTTP $health_code"
  exit 1
fi

printf '%s\n' "$target_commit" > "$STAGING_DIR/.deployed_commit"
log "deployed $target_commit; staging health HTTP $health_code"
