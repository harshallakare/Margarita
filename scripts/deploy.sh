#!/usr/bin/env bash
# Purpose: one-command install of Margarita (vmigrate) on a Linux VM/host as systemd services:
#          margarita-api (REST + web UI), margarita-conductor, margarita-worker.
#
# Usage (as root, on the VM):
#   curl -fsSL https://raw.githubusercontent.com/harshallakare/Margarita/main/scripts/deploy.sh | sudo bash
#   sudo bash scripts/deploy.sh [options]          # from a clone
#
# Options:
#   --port N            API/UI port (default 8080)
#   --bind ADDR         listen address (default 0.0.0.0)
#   --branch NAME       git branch to deploy (default main)
#   --repo URL          git repository (default https://github.com/harshallakare/Margarita.git)
#   --db-url URL        database (default sqlite:////var/lib/margarita/platform.db; postgresql+psycopg://... for HA)
#   --full              also install nbdkit, libguestfs-tools, cifs-utils (VMware VDDK reads, OSMorph, Hyper-V shares)
#   --no-web            skip building the web UI (API only)
#   --no-systemd        install everything but do not create/start services (containers, CI)
#   -h | --help
#
# What it does (idempotent — safe to re-run; never overwrites the secret key or existing users):
#   1. installs OS packages (apt or dnf/yum): git, Python >= 3.11, qemu-img, ssh client, ...
#   2. clones the repo to /opt/margarita/app, creates a venv at /opt/margarita/venv, pip installs
#   3. builds the web UI with Node (system Node >= 18, else a pinned, SHA-256-verified Node tarball)
#   4. creates /etc/margarita/{margarita.env, secret.key} and /var/lib/margarita (data)
#   5. initializes the DB schema, creates an `admin` user with a random password (first run only)
#   6. installs + starts systemd units, waits for /health
# Layout: app /opt/margarita/app · venv /opt/margarita/venv · config /etc/margarita · data /var/lib/margarita
# Security: api + conductor run as user `margarita`; the worker runs as root (it writes VHI vStorage
# backings, mounts shares, runs qemu-img/ssh). The secret key never leaves /etc/margarita (0640).
# Update later with: sudo bash /opt/margarita/app/scripts/update.sh

set -Eeuo pipefail

REPO="https://github.com/harshallakare/Margarita.git"
BRANCH="main"
PORT="8080"
BIND="0.0.0.0"
DB_URL=""
FULL=0
WEB=1
SYSTEMD=1
PREFIX="/opt/margarita"
APP="$PREFIX/app"
VENV="$PREFIX/venv"
TOOLS="$PREFIX/tools"
ETC="/etc/margarita"
DATA="/var/lib/margarita"
SVC_USER="margarita"
NODE_VERSION="22.12.0"     # pinned LTS; tarball verified against nodejs.org SHASUMS256.txt
EXTRAS="platform,nutanix,vhi,vmware,hyperv,openstack,aws"

log()  { printf '\033[1;34m==>\033[0m %s\n' "$*"; }
warn() { printf '\033[1;33mWARN:\033[0m %s\n' "$*" >&2; }
die()  { printf '\033[1;31mERROR:\033[0m %s\n' "$*" >&2; exit 1; }
trap 'die "deploy failed at line $LINENO (command: $BASH_COMMAND)"' ERR

while [ $# -gt 0 ]; do
  case "$1" in
    --port) PORT="$2"; shift 2 ;;
    --bind) BIND="$2"; shift 2 ;;
    --branch) BRANCH="$2"; shift 2 ;;
    --repo) REPO="$2"; shift 2 ;;
    --db-url) DB_URL="$2"; shift 2 ;;
    --full) FULL=1; shift ;;
    --no-web) WEB=0; shift ;;
    --no-systemd) SYSTEMD=0; shift ;;
    -h|--help) sed -n '2,32p' "$0" | sed 's/^# \{0,1\}//'; exit 0 ;;
    *) die "unknown option: $1 (see --help)" ;;
  esac
done

[ "$(id -u)" -eq 0 ] || die "run as root (sudo bash $0)"
[ "$(uname -s)" = "Linux" ] || die "Linux only"
case "$PORT" in ''|*[!0-9]*) die "--port must be a number" ;; esac

# ------------------------------------------------------------------ 1. OS packages
if command -v apt-get >/dev/null 2>&1; then PM=apt
elif command -v dnf >/dev/null 2>&1; then PM=dnf
elif command -v yum >/dev/null 2>&1; then PM=yum
else die "unsupported distribution: need apt, dnf or yum"; fi

pkg_install() {   # pkg_install required|optional pkg...
  local mode=$1; shift
  [ $# -gt 0 ] || return 0
  local rc=0
  if [ "$PM" = apt ]; then
    DEBIAN_FRONTEND=noninteractive apt-get install -y -q --no-install-recommends "$@" >/dev/null || rc=$?
  else
    "$PM" install -y -q "$@" >/dev/null || rc=$?
  fi
  if [ $rc -ne 0 ]; then
    if [ "$mode" = required ]; then die "could not install: $*"; fi
    warn "optional packages not installed: $* (install them later if you need those features)"
  fi
  return 0
}

missing() {   # missing cmd:pkg ...  -> the packages whose command is not already on PATH
  local pair out=()
  for pair in "$@"; do command -v "${pair%%:*}" >/dev/null 2>&1 || out+=("${pair#*:}"); done
  echo "${out[@]:-}"
}
# Only install what is missing: e.g. RHEL 9 images ship curl-minimal, which conflicts with the
# full `curl` package — any working curl is fine, so never swap system packages.
log "installing OS packages ($PM)"
if [ "$PM" = apt ]; then
  apt-get update -q >/dev/null
  # shellcheck disable=SC2046
  pkg_install required ca-certificates $(missing git:git curl:curl tar:tar xz:xz-utils ssh:openssh-client python3:python3)
  "${PY_PROBE:-python3}" -c 'import ensurepip' 2>/dev/null || pkg_install required python3-venv
  # shellcheck disable=SC2046
  pkg_install optional $(missing qemu-img:qemu-utils)
  # shellcheck disable=SC2046
  [ $FULL -eq 1 ] && pkg_install optional $(missing nbdkit:nbdkit guestfish:libguestfs-tools mount.cifs:cifs-utils)
else
  # shellcheck disable=SC2046
  pkg_install required ca-certificates $(missing git:git curl:curl tar:tar xz:xz ssh:openssh-clients python3:python3)
  # shellcheck disable=SC2046
  pkg_install optional $(missing qemu-img:qemu-img)
  # shellcheck disable=SC2046
  [ $FULL -eq 1 ] && pkg_install optional $(missing nbdkit:nbdkit guestfish:libguestfs-tools mount.cifs:cifs-utils)
fi

py_ok() { "$1" -c 'import sys; sys.exit(0 if sys.version_info >= (3, 11) else 1)' 2>/dev/null; }
find_python() {
  local c
  for c in python3.13 python3.12 python3.11 python3; do
    if command -v "$c" >/dev/null 2>&1 && py_ok "$(command -v "$c")"; then command -v "$c"; return 0; fi
  done
  return 1
}
PY="$(find_python || true)"
if [ -z "$PY" ]; then
  log "system Python is older than 3.11; installing a newer one"
  for v in 3.12 3.11; do
    if [ "$PM" = apt ]; then pkg_install optional "python$v" "python$v-venv"; else pkg_install optional "python$v"; fi
    PY="$(find_python || true)"; [ -n "$PY" ] && break
  done
fi
[ -n "$PY" ] || die "Python >= 3.11 is required and could not be installed (Ubuntu 22.04: enable 'universe'; RHEL 8: dnf module/AppStream python3.11)"
log "using $($PY --version) at $PY"

# ------------------------------------------------------------------ 2. code + venv
id "$SVC_USER" >/dev/null 2>&1 || useradd --system --home-dir "$DATA" --shell /usr/sbin/nologin "$SVC_USER"
mkdir -p "$PREFIX" "$TOOLS"
if [ -d "$APP/.git" ]; then
  log "app already present at $APP (use update.sh to change versions); keeping it"
else
  log "cloning $REPO ($BRANCH) to $APP"
  git clone --quiet --branch "$BRANCH" "$REPO" "$APP"
fi
git config --global --get-all safe.directory 2>/dev/null | grep -qx "$APP" || git config --global --add safe.directory "$APP"

if [ ! -x "$VENV/bin/python" ]; then
  log "creating virtualenv $VENV"
  "$PY" -m venv "$VENV"
fi
case "$DB_URL" in postgres*) EXTRAS="$EXTRAS,postgres" ;; esac
log "installing Python package (extras: $EXTRAS)"
"$VENV/bin/pip" install -q --upgrade pip
"$VENV/bin/pip" install -q --upgrade "$APP[$EXTRAS]"

# ------------------------------------------------------------------ 3. web UI
node_ok() { command -v node >/dev/null 2>&1 && node -e 'process.exit(+process.versions.node.split(".")[0] >= 18 ? 0 : 1)' 2>/dev/null; }
ensure_node() {
  if node_ok; then NODE_DIR="$(dirname "$(command -v node)")"; return; fi
  NODE_DIR="$TOOLS/node/bin"
  if [ -x "$NODE_DIR/node" ]; then return; fi
  local arch
  case "$(uname -m)" in x86_64) arch=x64 ;; aarch64|arm64) arch=arm64 ;; *) die "no Node build for $(uname -m); use --no-web" ;; esac
  local f="node-v$NODE_VERSION-linux-$arch.tar.xz" base="https://nodejs.org/dist/v$NODE_VERSION"
  local tmp; tmp="$(mktemp -d)"
  log "downloading Node $NODE_VERSION ($arch) for the UI build"
  curl -fsSL -o "$tmp/$f" "$base/$f"
  curl -fsSL -o "$tmp/SHASUMS256.txt" "$base/SHASUMS256.txt"
  (cd "$tmp" && grep " $f\$" SHASUMS256.txt | sha256sum -c --quiet -) || die "Node tarball checksum mismatch"
  rm -rf "$TOOLS/node" && mkdir -p "$TOOLS/node"
  tar -xJf "$tmp/$f" -C "$TOOLS/node" --strip-components=1
  rm -rf "$tmp"
}
if [ $WEB -eq 1 ]; then
  ensure_node
  log "building web UI (node $("$NODE_DIR/node" --version))"
  (cd "$APP/web" && PATH="$NODE_DIR:$PATH" npm ci --no-audit --no-fund --loglevel=error >/dev/null \
     && PATH="$NODE_DIR:$PATH" npm run build --silent >/dev/null)
fi

# ------------------------------------------------------------------ 4. config + data
mkdir -p "$ETC" "$DATA/reports"
chown root:"$SVC_USER" "$ETC"; chmod 0750 "$ETC"
chown "$SVC_USER":"$SVC_USER" "$DATA" "$DATA/reports"
chmod 2770 "$DATA" "$DATA/reports"           # setgid: root-written files (worker) stay group-writable
if [ ! -s "$ETC/secret.key" ]; then
  log "generating secret-store key $ETC/secret.key (BACK THIS UP: without it stored credentials are unreadable)"
  (umask 077 && "$VENV/bin/vmigrate-platform" gen-key > "$ETC/secret.key")
else
  log "keeping existing secret key"
fi
chown root:"$SVC_USER" "$ETC/secret.key"; chmod 0640 "$ETC/secret.key"

[ -n "$DB_URL" ] || DB_URL="sqlite:///$DATA/platform.db"
if [ ! -f "$ETC/margarita.env" ]; then
  cat > "$ETC/margarita.env" <<EOF
# Margarita runtime configuration (read by all three services). See README "Platform services".
VMIGRATE_DATABASE_URL=$DB_URL
VMIGRATE_SECRET_KEY_FILE=$ETC/secret.key
VMIGRATE_DATA_DIR=$DATA
VMIGRATE_WEB_DIR=$APP/web/dist
VMIGRATE_LEASE_SECONDS=60
VMIGRATE_POLL_SECONDS=2
EOF
else
  log "keeping existing $ETC/margarita.env"
fi
chown root:"$SVC_USER" "$ETC/margarita.env"; chmod 0640 "$ETC/margarita.env"

cat > "$ETC/install.conf" <<EOF
# written by deploy.sh; read by update.sh
APP=$APP
VENV=$VENV
TOOLS=$TOOLS
EXTRAS=$EXTRAS
PORT=$PORT
BIND=$BIND
BRANCH=$BRANCH
WEB=$WEB
SYSTEMD=$SYSTEMD
SVC_USER=$SVC_USER
EOF

# ------------------------------------------------------------------ 5. schema + admin user
run_as_svc() {   # run vmigrate-platform with the service environment, as the service user
  set -a; . "$ETC/margarita.env"; set +a
  if command -v runuser >/dev/null 2>&1; then
    runuser -u "$SVC_USER" --preserve-environment -- "$VENV/bin/vmigrate-platform" --log-level WARNING "$@"
  else
    su -s /bin/sh "$SVC_USER" -c "$VENV/bin/vmigrate-platform --log-level WARNING $*"
  fi
}
log "initializing database schema"
run_as_svc init-db >/dev/null

ADMIN_FILE="/root/margarita-admin-password"
has_admin=$( set -a; . "$ETC/margarita.env"; set +a; "$VENV/bin/python" - <<'PY'
from vmigrate.platform.factory import Services
from vmigrate.platform.settings import Settings
print(Services.from_settings(Settings.from_env()).repo.count_users())
PY
)
if [ "$has_admin" = "0" ]; then
  pw="$("$VENV/bin/python" -c 'import secrets; print(secrets.token_urlsafe(18))')"
  ( set -a; . "$ETC/margarita.env"; set +a; MARGARITA_ADMIN_PW="$pw" \
    "$VENV/bin/vmigrate-platform" --log-level WARNING create-user admin --role admin --password-env MARGARITA_ADMIN_PW >/dev/null )
  (umask 077 && printf 'admin\n%s\n' "$pw" > "$ADMIN_FILE")
  NEW_ADMIN=1
else
  NEW_ADMIN=0
fi
chown -R "$SVC_USER":"$SVC_USER" "$DATA"; chmod -R g+rwX "$DATA"

# ------------------------------------------------------------------ 6. systemd
if [ $SYSTEMD -eq 1 ]; then
  command -v systemctl >/dev/null 2>&1 || die "systemd not found (use --no-systemd and run the services yourself)"
  unit() {  # unit name description user exec
    cat > "/etc/systemd/system/$1.service" <<EOF
[Unit]
Description=$2
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
User=$3
Group=$SVC_USER
UMask=0007
EnvironmentFile=$ETC/margarita.env
ExecStart=$4
Restart=on-failure
RestartSec=5
NoNewPrivileges=$5

[Install]
WantedBy=multi-user.target
EOF
  }
  unit margarita-api "Margarita API + web UI" "$SVC_USER" "$VENV/bin/vmigrate-platform api --host $BIND --port $PORT" yes
  unit margarita-conductor "Margarita conductor" "$SVC_USER" "$VENV/bin/vmigrate-platform conductor" yes
  unit margarita-worker "Margarita worker (runs migrations; root for host storage access)" root \
       "$VENV/bin/vmigrate-platform worker" no
  systemctl daemon-reload
  systemctl enable --quiet margarita-api margarita-conductor margarita-worker
  systemctl restart margarita-api margarita-conductor margarita-worker
  log "waiting for the API on port $PORT"
  ok=0
  for _ in $(seq 1 60); do
    if curl -fsS "http://127.0.0.1:$PORT/health" >/dev/null 2>&1; then ok=1; break; fi
    sleep 1
  done
  [ $ok -eq 1 ] || die "API did not become healthy; check: journalctl -u margarita-api -n 50"
  if command -v firewall-cmd >/dev/null 2>&1 && firewall-cmd --state >/dev/null 2>&1; then
    warn "firewalld is active; to allow remote access: firewall-cmd --permanent --add-port=$PORT/tcp && firewall-cmd --reload"
  elif command -v ufw >/dev/null 2>&1 && ufw status 2>/dev/null | grep -q "Status: active"; then
    warn "ufw is active; to allow remote access: ufw allow $PORT/tcp"
  fi
fi

trap - ERR
HOST_IP="$(hostname -I 2>/dev/null | awk '{print $1}')"
echo
log "Margarita is installed ($(git -C "$APP" rev-parse --short HEAD) on $BRANCH)"
echo "    UI:        http://${HOST_IP:-<this-host>}:$PORT/ui/"
echo "    API docs:  http://${HOST_IP:-<this-host>}:$PORT/docs"
if [ "$NEW_ADMIN" = "1" ]; then
  echo "    Login:     admin / $pw"
  echo "               (also saved to $ADMIN_FILE, readable by root only; change it after first login)"
fi
echo "    Back up:   $ETC/secret.key  (without it, stored endpoint credentials cannot be decrypted)"
echo "    Logs:      journalctl -u margarita-api -u margarita-conductor -u margarita-worker -f"
echo "    Update:    sudo bash $APP/scripts/update.sh"
