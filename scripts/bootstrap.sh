#!/usr/bin/env bash
# Trading Intelligence System — DigitalOcean droplet bootstrap.
#
# Each step is a separate command. Run them in order and paste the output
# back so it can be reviewed before continuing.
#
# Usage:
#   scp scripts/bootstrap.sh root@<droplet-ip>:/root/
#   ssh root@<droplet-ip>
#   chmod +x /root/bootstrap.sh
#   /root/bootstrap.sh 1
#   /root/bootstrap.sh 2
#   ...
#
# Run as root. The script creates a non-privileged `trading` user and the
# application runs under that user — root holds nothing at runtime.

set -euo pipefail

APP_USER="trading"
APP_DIR="/app/profitlyworkflow"
REPO_URL="https://github.com/kimmonsruikka/profitlyworkflow.git"
PYTHON_BIN="python3.12"

require_root() {
    if [[ $EUID -ne 0 ]]; then
        echo "ERROR: must run as root" >&2
        exit 1
    fi
}

banner() {
    echo
    echo "============================================================"
    echo "  $1"
    echo "============================================================"
}


# ---------------------------------------------------------------------------
# Step 1 — Update apt and upgrade installed packages
# ---------------------------------------------------------------------------
step_1() {
    banner "Step 1: apt update + upgrade"
    apt-get update
    DEBIAN_FRONTEND=noninteractive apt-get upgrade -y
    cat <<MSG

Expected output:
  - 'Reading package lists... Done'
  - upgrade summary: '0 upgraded, 0 newly installed' on a fresh droplet,
    or a list of upgraded packages on a stale one.
  - no errors.
MSG
}


# ---------------------------------------------------------------------------
# Step 2 — Install system dependencies
# ---------------------------------------------------------------------------
step_2() {
    banner "Step 2: install python, git, redis, ufw, debian-keyring (for caddy)"
    DEBIAN_FRONTEND=noninteractive apt-get install -y \
        python3.12 python3.12-venv python3-pip \
        git curl ufw redis-server \
        debian-keyring debian-archive-keyring apt-transport-https
    systemctl enable redis-server
    systemctl start redis-server
    cat <<MSG

Expected output:
  - apt installs all listed packages without error.
  - 'redis-server' systemd service is enabled and active.
  - 'redis-cli ping' returns PONG (verify with: redis-cli ping).
MSG
}


# ---------------------------------------------------------------------------
# Step 3 — Configure firewall (open SSH, HTTP, HTTPS only)
# ---------------------------------------------------------------------------
step_3() {
    banner "Step 3: configure ufw — allow 22, 80, 443 only"
    ufw default deny incoming
    ufw default allow outgoing
    ufw allow 22/tcp comment 'ssh'
    ufw allow 80/tcp comment 'http (caddy)'
    ufw allow 443/tcp comment 'https (caddy)'
    yes | ufw enable
    ufw status verbose
    cat <<MSG

Expected output:
  - 'Status: active'
  - rules show 22, 80, 443 ALLOW IN; default DENY incoming.
  - port 8000 NOT in the allow list — application is loopback-only.
MSG
}


# ---------------------------------------------------------------------------
# Step 4 — Create the non-privileged 'trading' user
# ---------------------------------------------------------------------------
step_4() {
    banner "Step 4: create '$APP_USER' system user"
    if id -u "$APP_USER" >/dev/null 2>&1; then
        echo "user $APP_USER already exists — skipping useradd"
    else
        useradd --system --create-home --shell /bin/bash "$APP_USER"
    fi
    install -d -o "$APP_USER" -g "$APP_USER" -m 0755 /app
    cat <<MSG

Expected output:
  - 'id $APP_USER' returns a uid/gid (verify with: id $APP_USER).
  - /app exists and is owned by $APP_USER (verify with: ls -ld /app).
MSG
}


# ---------------------------------------------------------------------------
# Step 5 — Clone the repository as the 'trading' user
# ---------------------------------------------------------------------------
step_5() {
    banner "Step 5: clone repo into $APP_DIR (as $APP_USER)"
    if [[ -d "$APP_DIR/.git" ]]; then
        echo "$APP_DIR already a git checkout — pulling latest"
        sudo -u "$APP_USER" git -C "$APP_DIR" pull --ff-only
    else
        sudo -u "$APP_USER" git clone "$REPO_URL" "$APP_DIR"
    fi
    sudo -u "$APP_USER" git -C "$APP_DIR" rev-parse HEAD
    cat <<MSG

Expected output:
  - clone completes without error.
  - HEAD SHA printed at the end matches what you expect on main.
MSG
}


# ---------------------------------------------------------------------------
# Step 6 — Create the virtualenv and install requirements
# ---------------------------------------------------------------------------
step_6() {
    banner "Step 6: create venv and install requirements"
    sudo -u "$APP_USER" $PYTHON_BIN -m venv "$APP_DIR/venv"
    sudo -u "$APP_USER" "$APP_DIR/venv/bin/pip" install --upgrade pip
    sudo -u "$APP_USER" "$APP_DIR/venv/bin/pip" install -r "$APP_DIR/requirements.txt"
    cat <<MSG

Expected output:
  - venv created at $APP_DIR/venv/ owned by $APP_USER.
  - 'pip install' completes; final line names installed packages or 'Successfully installed ...'.
  - no resolution errors.

>>> STOP HERE before step 7.

You must create $APP_DIR/.env.production manually before continuing.
See the operator instructions you have separately.

The file must:
  - exist at $APP_DIR/.env.production
  - be owned by $APP_USER and chmod 600
  - contain DATABASE_URL pointing to the Managed PostgreSQL instance.
MSG
}


# ---------------------------------------------------------------------------
# Step 7 — Run database migrations
# ---------------------------------------------------------------------------
step_7() {
    banner "Step 7: run alembic migrations"
    if [[ ! -f "$APP_DIR/.env.production" ]]; then
        echo "ERROR: $APP_DIR/.env.production does not exist." >&2
        echo "Create it manually before running step 7." >&2
        exit 1
    fi
    sudo -u "$APP_USER" bash -c "
        set -a
        source '$APP_DIR/.env.production'
        set +a
        cd '$APP_DIR'
        ./venv/bin/alembic upgrade head
    "
    cat <<MSG

Expected output:
  - 'Running upgrade  -> 0001, initial schema'
  - 'Running upgrade 0001 -> 0002, add gate_decisions table'
  - no Postgres connection or permission errors.

After this completes, the 11 tables exist:
  tickers, promoter_entities, promoter_campaigns, promoter_network_edges,
  sec_filings, signals, trades, positions, account_state, price_data,
  gate_decisions
MSG
}


# ---------------------------------------------------------------------------
# Step 8 — Install systemd units (trading-app, edgar-watcher)
# ---------------------------------------------------------------------------
step_8() {
    banner "Step 8: install trading-app + edgar-watcher systemd units"

    cat >/etc/systemd/system/trading-app.service <<MSG
[Unit]
Description=Trading Intelligence System API
After=network.target redis-server.service

[Service]
Type=simple
User=$APP_USER
Group=$APP_USER
WorkingDirectory=$APP_DIR
EnvironmentFile=$APP_DIR/.env.production
ExecStart=$APP_DIR/venv/bin/uvicorn api.main:app --host 127.0.0.1 --port 8000
Restart=always
RestartSec=10

# Hardening
NoNewPrivileges=true
ProtectSystem=strict
ProtectHome=true
PrivateTmp=true
ReadWritePaths=$APP_DIR

[Install]
WantedBy=multi-user.target
MSG

    # edgar-watcher unit lives in the repo so deploys can update it.
    if [[ -f "$APP_DIR/deploy/edgar-watcher.service" ]]; then
        cp "$APP_DIR/deploy/edgar-watcher.service" /etc/systemd/system/edgar-watcher.service
    else
        echo "WARN: $APP_DIR/deploy/edgar-watcher.service not found — skipping" >&2
    fi

    systemctl daemon-reload
    systemctl enable trading-app
    systemctl start trading-app
    if [[ -f /etc/systemd/system/edgar-watcher.service ]]; then
        systemctl enable edgar-watcher
        systemctl start edgar-watcher
    fi
    sleep 2
    systemctl status trading-app --no-pager | head -10
    echo
    systemctl status edgar-watcher --no-pager 2>/dev/null | head -10 || true
    cat <<MSG

Expected output:
  - 'Active: active (running)' for trading-app.
  - 'Active: active (running)' for edgar-watcher (only after step 6 + .env.production are done).
  - User=$APP_USER on both (NOT root).
  - trading-app listens on 127.0.0.1:8000 (verify: ss -tlnp | grep 8000).

Note: edgar-watcher will log warnings until you run the universe seed
(scripts/seed_edgar_universe.py — ships in the next PR) and set
SEC_USER_AGENT in .env.production.
MSG
}


# ---------------------------------------------------------------------------
# Step 9 — Verify the local health endpoint
# ---------------------------------------------------------------------------
step_9() {
    banner "Step 9: curl http://127.0.0.1:8000/health"
    sleep 2
    curl -fsS http://127.0.0.1:8000/health
    echo
    cat <<MSG

Expected output:
  {"status":"ok","environment":"production","broker_mode":"paper","timestamp":"..."}
MSG
}


# ---------------------------------------------------------------------------
# Step 10 — Install Caddy and configure reverse proxy
# ---------------------------------------------------------------------------
step_10() {
    banner "Step 10: install Caddy + configure reverse proxy"

    if [[ -z "${DOMAIN:-}" ]]; then
        echo "ERROR: set DOMAIN before running step 10:" >&2
        echo "  DOMAIN=trading.example.com /root/bootstrap.sh 10" >&2
        echo "(Domain must already point to this droplet's IP.)" >&2
        exit 1
    fi

    if ! command -v caddy >/dev/null; then
        curl -1sLf 'https://dl.cloudsmith.io/public/caddy/stable/gpg.key' \
            | gpg --dearmor -o /usr/share/keyrings/caddy-stable-archive-keyring.gpg
        curl -1sLf 'https://dl.cloudsmith.io/public/caddy/stable/debian.deb.txt' \
            > /etc/apt/sources.list.d/caddy-stable.list
        apt-get update
        apt-get install -y caddy
    fi

    cat >/etc/caddy/Caddyfile <<MSG
$DOMAIN {
    encode gzip
    reverse_proxy 127.0.0.1:8000
}
MSG
    systemctl reload caddy || systemctl restart caddy
    systemctl status caddy --no-pager | head -10
    cat <<MSG

Expected output:
  - 'Active: active (running)' for caddy.
  - Caddy obtains a Let's Encrypt cert for $DOMAIN automatically (may take 30-60s).
  - 'curl https://$DOMAIN/health' returns the same JSON as step 9.
MSG
}


# ---------------------------------------------------------------------------
# Step 11 — Final verification
# ---------------------------------------------------------------------------
step_11() {
    banner "Step 11: end-to-end health checks"
    echo "Local (loopback only — should succeed):"
    curl -fsS http://127.0.0.1:8000/health || true
    echo
    echo
    echo "Public 8000 (should fail — port is firewalled):"
    timeout 3 bash -c 'cat </dev/tcp/0.0.0.0/8000' 2>&1 || echo "  (refused, as expected)"
    echo
    if [[ -n "${DOMAIN:-}" ]]; then
        echo "Public via Caddy at https://$DOMAIN/health:"
        curl -fsS "https://$DOMAIN/health" || true
        echo
    fi
    echo
    echo "Active services:"
    systemctl is-active redis-server trading-app caddy
    cat <<MSG

Expected output:
  - loopback /health returns the JSON.
  - port 8000 not reachable from outside (good).
  - https://$DOMAIN/health (if DOMAIN set) returns the same JSON over TLS.
  - all three services report 'active'.
MSG
}


# ---------------------------------------------------------------------------
# Dispatcher
# ---------------------------------------------------------------------------
require_root

case "${1:-}" in
    1)  step_1  ;;
    2)  step_2  ;;
    3)  step_3  ;;
    4)  step_4  ;;
    5)  step_5  ;;
    6)  step_6  ;;
    7)  step_7  ;;
    8)  step_8  ;;
    9)  step_9  ;;
    10) step_10 ;;
    11) step_11 ;;
    *)
        cat <<MSG
Usage: $0 <step-number>

Steps:
  1   apt update + upgrade
  2   install python, git, redis, ufw, caddy keyring
  3   configure ufw (22, 80, 443 only)
  4   create the 'trading' system user and /app
  5   clone the repo as 'trading'
  6   create venv + pip install (then STOP and create .env.production manually)
  7   run alembic migrations
  8   install + start trading-app and edgar-watcher systemd units
  9   curl localhost /health
  10  install caddy + reverse-proxy <DOMAIN>:443 -> 127.0.0.1:8000
        DOMAIN=trading.example.com $0 10
  11  end-to-end verification
MSG
        exit 1
        ;;
esac
