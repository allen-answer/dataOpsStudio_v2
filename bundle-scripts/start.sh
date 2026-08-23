#!/usr/bin/env bash
# First-run initialization followed by API + worker + managed PostgreSQL.

set -Eeuo pipefail
source "$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd -P)/_common.sh"

step() { printf '[start] %s\n' "$*"; }

if [[ ! -x "$VENV_PY" ]]; then
    step 'first run: preparing Python environment'
    "$BUNDLE_ROOT/install.sh"
fi

initialized_marker="$DATAOPS_HOME/config/.bundle-initialized"
if [[ ! -f "$initialized_marker" ]]; then
    step 'first run: generating bootstrap secrets'
    invoke_launcher bootstrap init
    step 'first run: starting metadata PostgreSQL'
    invoke_launcher pg-up
    step 'first run: applying database migrations'
    invoke_launcher alembic-up
    step "first run: creating admin user $DATAOPS_ADMIN_USER"
    if [[ -n "${DATAOPS_ADMIN_PASSWORD:-}" ]]; then
        invoke_launcher admin create \
            --username "$DATAOPS_ADMIN_USER" \
            --password "$DATAOPS_ADMIN_PASSWORD" \
            --update-password
    else
        invoke_launcher admin create --username "$DATAOPS_ADMIN_USER" --update-password
    fi
    touch -- "$initialized_marker"
fi

unset DATAOPS_ADMIN_PASSWORD
step "launching DataOpsStudio on http://127.0.0.1:$DATAOPS_API_PORT/"
invoke_launcher up
