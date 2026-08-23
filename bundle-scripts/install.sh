#!/usr/bin/env bash
# Idempotently build the offline venv from bundled, hash-pinned wheels.

set -Eeuo pipefail
source "$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd -P)/_common.sh"

step() { printf '[install] %s\n' "$*"; }
die() { printf '[install] ERROR: %s\n' "$*" >&2; exit 1; }

socket_path="$DATAOPS_HOME/data/pg-socket/.s.PGSQL.$DATAOPS_PG_PORT"
socket_bytes="$(LC_ALL=C printf '%s' "$socket_path" | wc -c | tr -d ' ')"
if (( socket_bytes > 107 )); then
    die "PostgreSQL Unix socket path is $socket_bytes bytes (>107): $socket_path. Set DATAOPS_HOME to a shorter path."
fi

[[ -x "$PYTHON" ]] || die "bundled Python 3.12 missing: $PYTHON"
[[ -x "$UV" ]] || die "bundled uv missing: $UV"
[[ -x "$PG_BIN/postgres" ]] || die "bundled PostgreSQL missing: $PG_BIN/postgres"
[[ -f "$REQS" ]] || die "frozen requirements missing: $REQS"
[[ -d "$WHEELS" ]] || die "offline wheels missing: $WHEELS"

if [[ -x "$VENV_PY" ]]; then
    step "venv already built: $VENV"
    exit 0
fi

step "creating venv with bundled Python 3.12"
"$PYTHON" -m venv "$VENV"
step "installing hash-pinned dependencies from bundled wheels (--no-index)"
"$VENV_PY" -m pip install \
    --no-index \
    --find-links "$WHEELS" \
    --require-hashes \
    -r "$REQS"
step 'environment ready'
