#!/usr/bin/env bash
# Shared runtime contract for the DataOpsStudio Linux Mint offline bundle.

set -Eeuo pipefail

BUNDLE_ROOT="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd -P)"
# __DATAOPS_BUILD_IDENTITY__

# Debian installs application resources below /usr/lib, which is intentionally
# read-only for normal users. Keep the immutable bundle there and place the
# generated venv and instance data below XDG_DATA_HOME. Portable/extracted runs
# can opt back into bundle-local state with DATAOPS_STATE_ROOT=$BUNDLE_ROOT.
: "${DATAOPS_STATE_ROOT:=${XDG_DATA_HOME:-$HOME/.local/share}/dataops-studio}"
: "${DATAOPS_HOME:=$DATAOPS_STATE_ROOT/home}"
: "${DATAOPS_API_PORT:=8020}"
: "${DATAOPS_PG_PORT:=15432}"
: "${DATAOPS_ADMIN_USER:=admin}"

PY_DIR="$BUNDLE_ROOT/runtime/python"
PYTHON="$PY_DIR/bin/python3"
UV="$BUNDLE_ROOT/runtime/uv/uv"
VENV="$DATAOPS_STATE_ROOT/.venv"
VENV_PY="$VENV/bin/python"
PG_BIN="$BUNDLE_ROOT/pgsql/bin"
WHEELS="$BUNDLE_ROOT/runtime/wheels"
REQS="$BUNDLE_ROOT/runtime/requirements-frozen.txt"

export DATAOPS_STATE_ROOT DATAOPS_HOME DATAOPS_API_PORT DATAOPS_PG_PORT
export DATAOPS_ADMIN_USER
export UV_OFFLINE=1
export UV_NO_SYNC=1
export UV_FROZEN=1
export UV_PYTHON_DOWNLOADS=0
export UV_PROJECT_ENVIRONMENT="$VENV"
export DATAOPS_FORM=portable
export DATAOPS_FRONTEND_DIST="$BUNDLE_ROOT/frontend/dist"
export PATH="$PG_BIN:$PATH"
export LD_LIBRARY_PATH="$BUNDLE_ROOT/pgsql/lib${LD_LIBRARY_PATH:+:$LD_LIBRARY_PATH}"

mkdir -p -- "$DATAOPS_STATE_ROOT"
cd -- "$BUNDLE_ROOT"

invoke_launcher() {
    "$VENV_PY" -m app.launcher \
        --root "$DATAOPS_HOME" \
        --pg-bin-dir "$PG_BIN" \
        --uv "$UV" \
        "$@"
}
