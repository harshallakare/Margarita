#!/usr/bin/env bash
# Purpose: update a Margarita install made by deploy.sh to the latest commit of its branch:
#          pull -> reinstall -> rebuild UI (if changed) -> upgrade DB schema -> restart -> health check,
#          with automatic ROLLBACK to the previous commit if the new version does not come up healthy.
#
# Usage (as root on the VM):
#   sudo bash /opt/margarita/app/scripts/update.sh [options]
#
# Options:
#   --check             only show what would change (no changes)
#   --force             update even if executions are running or the checkout has local edits
#                       (running executions resume after the restart: they are lease-based)
#   --wait-timeout SEC  how long to wait for running executions to finish first (default 1800)
#   --ref REF           deploy a specific commit/tag/branch instead of the tip of the configured branch
#   -h | --help
#
# Safety:
#   * refuses to run if /opt/margarita/app has local modifications (unless --force)
#   * only fast-forwards (never merges or rewrites history on the VM)
#   * waits for running migrations/syncs to finish before restarting the worker
#   * schema upgrades only ADD columns/tables, so a rollback to the previous code still works
#   * the secret key, config and data are never touched

set -Eeuo pipefail

ETC="/etc/margarita"
CHECK=0
FORCE=0
WAIT_TIMEOUT=1800
REF=""

log()  { printf '\033[1;34m==>\033[0m %s\n' "$*"; }
warn() { printf '\033[1;33mWARN:\033[0m %s\n' "$*" >&2; }
die()  { printf '\033[1;31mERROR:\033[0m %s\n' "$*" >&2; exit 1; }

while [ $# -gt 0 ]; do
  case "$1" in
    --check) CHECK=1; shift ;;
    --force) FORCE=1; shift ;;
    --wait-timeout) WAIT_TIMEOUT="$2"; shift 2 ;;
    --ref) REF="$2"; shift 2 ;;
    -h|--help) sed -n '2,24p' "$0" | sed 's/^# \{0,1\}//'; exit 0 ;;
    *) die "unknown option: $1 (see --help)" ;;
  esac
done

[ "$(id -u)" -eq 0 ] || die "run as root (sudo bash $0)"
[ -f "$ETC/install.conf" ] || die "$ETC/install.conf not found: install with deploy.sh first"
# shellcheck disable=SC1091
. "$ETC/install.conf"
cd "$APP"
git config --global --get-all safe.directory 2>/dev/null | grep -qx "$APP" || git config --global --add safe.directory "$APP"

# ------------------------------------------------------------------ what would change
if [ -n "$(git status --porcelain --untracked-files=no)" ]; then
  if [ $FORCE -eq 1 ]; then
    warn "local modifications in $APP will be stashed"
  else
    git status --short --untracked-files=no
    die "local modifications in $APP; commit/revert them, or re-run with --force (they will be stashed)"
  fi
fi

OLD="$(git rev-parse HEAD)"
log "fetching from $(git remote get-url origin)"
git fetch --quiet --tags origin
if [ -n "$REF" ]; then
  NEW="$(git rev-parse --verify "$REF^{commit}" 2>/dev/null || git rev-parse --verify "origin/$REF^{commit}")" \
    || die "unknown ref $REF"
else
  NEW="$(git rev-parse "origin/$BRANCH")"
fi

if [ "$OLD" = "$NEW" ]; then
  log "already up to date ($(git rev-parse --short HEAD))"
  exit 0
fi
if [ -z "$REF" ] && ! git merge-base --is-ancestor "$OLD" "$NEW"; then
  die "origin/$BRANCH is not a fast-forward of the installed commit $(git rev-parse --short "$OLD"); use --ref to choose explicitly"
fi

echo "Installed: $(git log -1 --format='%h %s' "$OLD")"
echo "Target:    $(git log -1 --format='%h %s' "$NEW")"
echo "Changes:"
git log --oneline "$OLD..$NEW" 2>/dev/null | sed 's/^/  /' || true
WEB_CHANGED=0
git diff --quiet "$OLD" "$NEW" -- web/ || WEB_CHANGED=1
[ $CHECK -eq 1 ] && { log "--check: nothing changed"; exit 0; }

# ------------------------------------------------------------------ wait for running work
set -a; . "$ETC/margarita.env"; set +a
running() {
  "$VENV/bin/python" - <<'PY' 2>/dev/null || echo "?"
from vmigrate.platform.factory import Services
from vmigrate.platform.settings import Settings
r = Services.from_settings(Settings.from_env(), init_schema=False).repo
print(sum(len(r.list_executions(status=s)) for s in ("queued", "running")))
PY
}
if [ $FORCE -eq 0 ]; then
  waited=0
  while true; do
    n="$(running)"
    [ "$n" = "0" ] && break
    [ "$n" = "?" ] && { warn "could not query running executions; continuing"; break; }
    if [ "$waited" -ge "$WAIT_TIMEOUT" ]; then
      die "$n execution(s) still queued/running after ${WAIT_TIMEOUT}s; re-run later or use --force (they resume after restart)"
    fi
    [ $((waited % 60)) -eq 0 ] && log "waiting for $n running execution(s) to finish (up to ${WAIT_TIMEOUT}s)"
    sleep 10; waited=$((waited + 10))
  done
fi

# ------------------------------------------------------------------ apply
case "${ROLE:-all}" in
  server) SERVICES="margarita-api margarita-conductor" ;;
  worker) SERVICES="margarita-worker" ;;
  *) SERVICES="margarita-api margarita-conductor margarita-worker" ;;
esac
[ -n "${EXPORT_PORT:-}" ] && SERVICES="$SERVICES margarita-export"
node_dir() {
  if command -v node >/dev/null 2>&1 && node -e 'process.exit(+process.versions.node.split(".")[0] >= 18 ? 0 : 1)'; then
    dirname "$(command -v node)"
  elif [ -x "$TOOLS/node/bin/node" ]; then
    echo "$TOOLS/node/bin"
  else
    return 1
  fi
}

apply() {   # apply <commit> <rebuild-web 0|1>
  local commit=$1 web=$2
  # Every step checks its own exit status: this function runs under `if`, where bash disables
  # `set -e` for the whole call chain (subshells included), so set -e must not be relied on here.
  if [ -n "$REF" ]; then
    git -c advice.detachedHead=false checkout --quiet "$commit" || return 1
  else
    git checkout --quiet -B "$BRANCH" "$commit" || return 1   # move the branch (forward, or back on rollback)
  fi
  log "installing Python package"
  "$VENV/bin/pip" install -q --upgrade "$APP[$EXTRAS]" || { warn "pip install failed"; return 1; }
  if [ "$WEB" = "1" ] && { [ "$web" = "1" ] || [ ! -f "$APP/web/dist/index.html" ]; }; then
    local nd
    if nd="$(node_dir)"; then
      log "rebuilding web UI"
      (cd "$APP/web" && PATH="$nd:$PATH" npm ci --no-audit --no-fund --loglevel=error >/dev/null \
         && PATH="$nd:$PATH" npm run build --silent >/dev/null) || { warn "web UI build failed"; return 1; }
    else
      warn "Node not found; skipping UI build (re-run deploy.sh to install it)"
    fi
  fi
  log "upgrading database schema"
  "$VENV/bin/vmigrate-platform" --log-level WARNING init-db >/dev/null || { warn "schema upgrade failed"; return 1; }
  chown -R "$SVC_USER":"$SVC_USER" "$VMIGRATE_DATA_DIR" 2>/dev/null || true
  return 0
}

healthy() {
  [ "$SYSTEMD" = "1" ] || return 0
  # shellcheck disable=SC2086
  systemctl restart $SERVICES
  for _ in $(seq 1 60); do
    local all_up=1 s
    for s in $SERVICES; do systemctl is-active --quiet "$s" || all_up=0; done
    if [ $all_up -eq 1 ] && { [ "${ROLE:-all}" = worker ] || curl -fsS "http://127.0.0.1:$PORT/health" >/dev/null 2>&1; }; then
      return 0
    fi
    sleep 1
  done
  return 1
}

[ $FORCE -eq 1 ] && [ -n "$(git status --porcelain --untracked-files=no)" ] && git stash push --quiet -m "update.sh $(date -u +%FT%TZ)"

attempt() {   # attempt <commit>  -> 0 only if installed AND healthy (apply checks each step itself)
  apply "$1" "$WEB_CHANGED" || return 1
  healthy
}

if attempt "$NEW"; then
  log "updated $(git rev-parse --short "$OLD") -> $(git rev-parse --short "$NEW") and healthy"
  exit 0
fi
warn "new version failed (install or health check); rolling back to $(git rev-parse --short "$OLD")"
journalctl -u margarita-api -n 20 --no-pager 2>/dev/null || true
if attempt "$OLD"; then
  die "rolled back to $(git rev-parse --short "$OLD"); the service is running the previous version. Inspect the logs above."
fi
die "rollback ALSO failed; check: journalctl -u margarita-api -u margarita-worker -n 100"
