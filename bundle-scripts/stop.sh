#!/usr/bin/env bash
# Graceful stop. Pass --force for the launcher's immediate-stop path.

set -Eeuo pipefail
source "$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd -P)/_common.sh"

printf '[stop] stopping DataOpsStudio (graceful; active queries drain) ...\n'
if [[ ! -x "$VENV_PY" ]]; then
    printf '[stop] environment is not installed; nothing to stop.\n'
    exit 0
fi
invoke_launcher stop "$@"
