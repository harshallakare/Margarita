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
#   --full              also install nbdkit, libguestfs-tools, virt-v2v, virtio-win (RHEL), cifs-utils
#                       (VMware VDDK reads, Linux + Windows OSMorph, Hyper-V shares)
#   --no-web            skip building the web UI (API only)
#   --no-systemd        install everything but do not create/start services (containers, CI)
#   --role ROLE         all (default) | server (api + conductor) | worker (worker only: joins a central
#                       server; needs --db-url postgresql+psycopg://... and --secret-key-file)
#   --region NAME       this worker's region/site (routing: it only takes jobs for that region)
#   --worker-tags LIST  extra worker capability tags, comma-separated (e.g. vstorage,site-a)
#   --secret-key-file F copy the central server's secret.key (required for --role worker)
#   --pool NAME         join worker pool NAME (adds the worker tag pool:NAME)
#   --enroll URL        join a central server as a worker with a single-use token (with --enroll-token):
#                       fetches the database URL, secret key, region and pool from URL/enroll over HTTPS
#   --enroll-token T    the token from the UI (Settings > Worker pools) or POST /pools/{id}/enroll-token
#   --export-agent PORT also run the export agent (HTTPS on PORT, self-signed cert pinned by peers):
#                       serves this site's source disks to workers at other sites (needs --region)
#   -h | --help
#
# What it does (idempotent — safe to re-run; never overwrites the secret key or existing users):
#   1. installs OS packages (apt or dnf/yum): git, Python >= 3.11, qemu-img, ssh client, iSCSI initiator, ...
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
ROLE="all"
EXPORT_PORT=""
POOL=""
ENROLL_URL=""
ENROLL_TOKEN=""
REGION=""
WORKER_TAGS=""
KEY_FILE=""
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
    --role) ROLE="$2"; shift 2 ;;
    --region) REGION="$2"; shift 2 ;;
    --export-agent) EXPORT_PORT="$2"; shift 2 ;;
    --pool) POOL="$2"; shift 2 ;;
    --enroll) ENROLL_URL="$2"; shift 2 ;;
    --enroll-token) ENROLL_TOKEN="$2"; shift 2 ;;
    --worker-tags) WORKER_TAGS="$2"; shift 2 ;;
    --secret-key-file) KEY_FILE="$2"; shift 2 ;;
    -h|--help) sed -n '2,32p' "$0" | sed 's/^# \{0,1\}//'; exit 0 ;;
    *) die "unknown option: $1 (see --help)" ;;
  esac
done

[ "$(id -u)" -eq 0 ] || die "run as root (sudo bash $0)"
case "$ROLE" in all|server|worker) ;; *) die "--role must be all, server or worker" ;; esac
if [ -n "$EXPORT_PORT" ]; then
  [ -n "$REGION" ] || die "--export-agent needs --region (peers find the agent by its region)"
  [ "$ROLE" != server ] || die "--export-agent runs next to a worker: use --role all or worker"
fi
if [ -n "$ENROLL_URL" ] || [ -n "$ENROLL_TOKEN" ]; then
  [ -n "$ENROLL_URL" ] && [ -n "$ENROLL_TOKEN" ] || die "--enroll and --enroll-token go together"
  [ -f /etc/margarita/install.conf ] && die "this host already runs Margarita; enrollment is for new workers"
  ROLE=worker
fi
if [ "$ROLE" = worker ] && [ -z "$ENROLL_URL" ]; then
  WEB=0
  case "$DB_URL" in postgres*) ;; *) [ -f /etc/margarita/margarita.env ] || die "--role worker needs --db-url postgresql+psycopg://... (the central database)" ;; esac
  [ -n "$KEY_FILE" ] || [ -s /etc/margarita/secret.key ] || die "--role worker needs --secret-key-file (copy of the server's /etc/margarita/secret.key)"
fi
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
  pkg_install optional $(missing qemu-img:qemu-utils iscsiadm:open-iscsi)
  # shellcheck disable=SC2046
  [ $FULL -eq 1 ] && pkg_install optional $(missing nbdkit:nbdkit guestfish:libguestfs-tools mount.cifs:cifs-utils)
  # shellcheck disable=SC2046
  [ $FULL -eq 1 ] && pkg_install optional $(missing virt-v2v:virt-v2v)
else
  # shellcheck disable=SC2046
  pkg_install required ca-certificates $(missing git:git curl:curl tar:tar xz:xz ssh:openssh-clients python3:python3)
  # shellcheck disable=SC2046
  pkg_install optional $(missing qemu-img:qemu-img iscsiadm:iscsi-initiator-utils)
  # shellcheck disable=SC2046
  [ $FULL -eq 1 ] && pkg_install optional $(missing nbdkit:nbdkit guestfish:libguestfs-tools mount.cifs:cifs-utils)
  # shellcheck disable=SC2046
  [ $FULL -eq 1 ] && pkg_install optional $(missing virt-v2v:virt-v2v)
  # Windows OSMorphing drivers (RHEL AppStream; elsewhere download virtio-win.iso and set VIRTIO_WIN)
  [ $FULL -eq 1 ] && [ ! -e /usr/share/virtio-win ] && pkg_install optional virtio-win
fi

# ------------------------------------------------------------------ 1b. enrollment (new pool workers)
if [ -n "$ENROLL_URL" ]; then
  WEB=0
  log "enrolling with $ENROLL_URL"
  ENROLL_JSON=$(umask 077 && mktemp)
  ENROLL_KEY=$(umask 077 && mktemp)
  # shellcheck disable=SC2064
  trap "rm -f '$ENROLL_JSON' '$ENROLL_KEY'" EXIT
  python3 - "$ENROLL_URL" "$ENROLL_TOKEN" "$(hostname 2>/dev/null || cat /etc/hostname)" "$ENROLL_JSON" <<'PYEOF' || die "enrollment failed (see above)"
import json, sys, urllib.request, urllib.error
url, tok, host, out = sys.argv[1].rstrip("/") + "/enroll", sys.argv[2], sys.argv[3], sys.argv[4]
req = urllib.request.Request(url, data=json.dumps({"token": tok, "hostname": host}).encode(),
                             headers={"Content-Type": "application/json"})
try:
    body = urllib.request.urlopen(req, timeout=60).read()
except urllib.error.HTTPError as e:
    print(f"enroll: HTTP {e.code}: {e.read().decode(errors='replace')[:300]}", file=sys.stderr); sys.exit(1)
open(out, "wb").write(body)
PYEOF
  DB_URL=$(python3 -c 'import json,sys; print(json.load(open(sys.argv[1]))["database_url"])' "$ENROLL_JSON")
  python3 -c 'import json,sys; open(sys.argv[2],"w").write(json.load(open(sys.argv[1]))["secret_key"])' "$ENROLL_JSON" "$ENROLL_KEY"
  KEY_FILE="$ENROLL_KEY"
  [ -n "$REGION" ] || REGION=$(python3 -c 'import json,sys; print(json.load(open(sys.argv[1])).get("region") or "")' "$ENROLL_JSON")
  ETAGS=$(python3 -c 'import json,sys; print(",".join(json.load(open(sys.argv[1])).get("tags") or []))' "$ENROLL_JSON")
  [ -n "$ETAGS" ] && WORKER_TAGS="${WORKER_TAGS:+$WORKER_TAGS,}$ETAGS"
  log "enrolled: region ${REGION:-none}, tags ${ETAGS:-none}"
fi
if [ -n "$POOL" ]; then
  WORKER_TAGS="${WORKER_TAGS:+$WORKER_TAGS,}pool:$POOL"
fi

# Nutanix iSCSI transport: the initiator daemon must run on workers (no-op without systemd/iscsiadm)
if command -v iscsiadm >/dev/null 2>&1 && command -v systemctl >/dev/null 2>&1; then
  systemctl enable --now iscsid >/dev/null 2>&1 || systemctl enable --now iscsid.socket >/dev/null 2>&1 || true
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
if [ -n "$KEY_FILE" ] && [ ! -s "$ETC/secret.key" ]; then
  log "installing the shared secret-store key from $KEY_FILE"
  (umask 077 && cp "$KEY_FILE" "$ETC/secret.key")
elif [ ! -s "$ETC/secret.key" ]; then
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
set_env() {   # set_env KEY VALUE  (replace or append in margarita.env)
  if grep -q "^$1=" "$ETC/margarita.env"; then sed -i "s|^$1=.*|$1=$2|" "$ETC/margarita.env"
  else echo "$1=$2" >> "$ETC/margarita.env"; fi
}
[ -n "$REGION" ] && set_env VMIGRATE_WORKER_REGION "$REGION"
[ -n "$WORKER_TAGS" ] && set_env VMIGRATE_WORKER_TAGS "$WORKER_TAGS"
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
ROLE=$ROLE
EXPORT_PORT=$EXPORT_PORT
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
NEW_ADMIN=0
[ "$ROLE" = worker ] && has_admin=skip || has_admin=$( set -a; . "$ETC/margarita.env"; set +a; "$VENV/bin/python" - <<'PY'
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
  case "$ROLE" in
    all) UNITS="margarita-api margarita-conductor margarita-worker" ;;
    server) UNITS="margarita-api margarita-conductor" ;;
    worker) UNITS="margarita-worker" ;;
  esac
  if [ -n "$EXPORT_PORT" ]; then
    unit margarita-export "Margarita export agent (serves source disks to other sites, HTTPS :$EXPORT_PORT)" root \
         "$VENV/bin/vmigrate-platform export-agent --port $EXPORT_PORT" no
    UNITS="$UNITS margarita-export"
    log "export agent on :$EXPORT_PORT — open this port to the other sites' workers only"
  fi
  systemctl daemon-reload
  # shellcheck disable=SC2086
  systemctl enable --quiet $UNITS
  # shellcheck disable=SC2086
  systemctl restart $UNITS
  ok=0
  if [ "$ROLE" = worker ]; then
    log "waiting for the worker service"
    for _ in $(seq 1 30); do systemctl is-active --quiet margarita-worker && { ok=1; break; }; sleep 1; done
    [ $ok -eq 1 ] || die "worker did not start; check: journalctl -u margarita-worker -n 50"
  else
    log "waiting for the API on port $PORT"
    for _ in $(seq 1 60); do
      if curl -fsS "http://127.0.0.1:$PORT/health" >/dev/null 2>&1; then ok=1; break; fi
      sleep 1
    done
    [ $ok -eq 1 ] || die "API did not become healthy; check: journalctl -u margarita-api -n 50"
  fi
  if command -v firewall-cmd >/dev/null 2>&1 && firewall-cmd --state >/dev/null 2>&1; then
    warn "firewalld is active; to allow remote access: firewall-cmd --permanent --add-port=$PORT/tcp && firewall-cmd --reload"
  elif command -v ufw >/dev/null 2>&1 && ufw status 2>/dev/null | grep -q "Status: active"; then
    warn "ufw is active; to allow remote access: ufw allow $PORT/tcp"
  fi
fi

trap - ERR
# Display only — must never fail the install: minimal RHEL-family images have no `hostname` binary,
# and with `set -o pipefail` a missing command here would abort the script after a good install.
HOST_IP="$( { hostname -I 2>/dev/null || ip -4 route get 1.1.1.1 2>/dev/null | awk '{for (i=1;i<NF;i++) if ($i=="src") print $(i+1)}' \
            || awk '/32 host/ {print f} {f=$2}' /proc/net/fib_trie 2>/dev/null | grep -v '^127\.'; } | awk 'NF {print $1; exit}' || true )"
echo
log "Margarita ($ROLE) is installed ($(git -C "$APP" rev-parse --short HEAD) on $BRANCH)${REGION:+, region $REGION}"
echo "    UI:        http://${HOST_IP:-<this-host>}:$PORT/ui/"
echo "    API docs:  http://${HOST_IP:-<this-host>}:$PORT/docs"
if [ "$NEW_ADMIN" = "1" ]; then
  echo "    Login:     admin / $pw"
  echo "               (also saved to $ADMIN_FILE, readable by root only; change it after first login)"
fi
echo "    Back up:   $ETC/secret.key  (without it, stored endpoint credentials cannot be decrypted)"
echo "    Logs:      journalctl -u margarita-api -u margarita-conductor -u margarita-worker -f"
echo "    Update:    sudo bash $APP/scripts/update.sh"
