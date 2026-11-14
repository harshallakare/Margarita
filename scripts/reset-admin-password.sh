#!/usr/bin/env bash
# Purpose: reset a forgotten Margarita password (normally the admin's) on the server, from the shell.
#          Runs `vmigrate-platform reset-password` with the service environment; the account is
#          re-enabled and every session of that user is signed out.
#
# Usage (as root on the Margarita server):
#   sudo bash /opt/margarita/app/scripts/reset-admin-password.sh                 # user "admin", prompts twice
#   sudo bash /opt/margarita/app/scripts/reset-admin-password.sh alice           # another user
#   sudo bash /opt/margarita/app/scripts/reset-admin-password.sh admin --generate   # random password, printed once
#
# Needs: the install made by deploy.sh (/etc/margarita/install.conf + margarita.env). The new password is
# never written to disk or to the shell history; only its scrypt hash goes into the database.

set -Eeuo pipefail
ETC="/etc/margarita"
die() { printf '\033[1;31mERROR:\033[0m %s\n' "$*" >&2; exit 1; }

[ "$(id -u)" -eq 0 ] || die "run as root (sudo): the service environment holds the database URL and secret key"
[ -f "$ETC/install.conf" ] || die "$ETC/install.conf not found: is Margarita installed here with deploy.sh?"
[ -f "$ETC/margarita.env" ] || die "$ETC/margarita.env not found"

USER_NAME="${1:-admin}"
[ $# -gt 0 ] && shift
case "$USER_NAME" in -*) die "first argument is the user name (default admin), e.g.: $0 admin --generate" ;; esac

VENV="$(sed -n 's/^VENV=//p' "$ETC/install.conf")"
SVC_USER="$(sed -n 's/^SVC_USER=//p' "$ETC/install.conf")"
VENV="${VENV:-/opt/margarita/venv}"
SVC_USER="${SVC_USER:-margarita}"
[ -x "$VENV/bin/vmigrate-platform" ] || die "$VENV/bin/vmigrate-platform not found"

set -a; . "$ETC/margarita.env"; set +a
cd /

# run as the service user (same as the API), so a SQLite database keeps its ownership
if command -v runuser >/dev/null 2>&1 && id "$SVC_USER" >/dev/null 2>&1; then
  exec runuser -u "$SVC_USER" --preserve-environment -- "$VENV/bin/vmigrate-platform" --log-level WARNING reset-password "$USER_NAME" "$@"
fi
exec "$VENV/bin/vmigrate-platform" --log-level WARNING reset-password "$USER_NAME" "$@"
