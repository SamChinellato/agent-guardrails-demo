#!/usr/bin/env bash
# Open the APISIX admin UI through a deliberate port-forward (rule 6, D-025).
# Listens on 127.0.0.1 only; stop with Ctrl-C.
set -euo pipefail
source "$(dirname "${BASH_SOURCE[0]}")/lib.sh"
PORT="${ADMIN_UI_PORT:-9180}"
cat <<MSG
APISIX admin UI: http://localhost:$PORT/ui/
  Read-only key (viewer): see $ROOT/.secrets/apisix-viewer-key
  Admin key (can change config; bootstrap-gateway.sh reports drift): $ROOT/.secrets/apisix-admin-key
Ctrl-C to close.
MSG
exec kubectl -n gateway port-forward --address 127.0.0.1 svc/apisix-admin "$PORT:9180"
