#!/usr/bin/env bash
# Open one of the secondary UIs while this command runs (Ctrl-C to close).
#   prometheus | alertmanager | evidence   always on since D-042; this prints their addresses
#   apisix                                 port-forward, this machine only (Admin API port, rule 6)
set -euo pipefail
source "$(dirname "${BASH_SOURCE[0]}")/lib.sh"
ui="${1:-}"
ip="$( (hostname -I 2>/dev/null || ipconfig getifaddr en0 2>/dev/null) | awk '{print $1}')"
where() { echo "  this machine: http://localhost:$1$2"; [[ "$HOST_BIND" == 0.0.0.0 && -n "$ip" ]] && echo "  from the LAN: http://$ip:$1$2"; return 0; }
case "$ui" in
  prometheus)   echo "Prometheus (always on):";   where "$HOST_PORT_PROMETHEUS" /alerts; exit 0 ;;
  alertmanager) echo "Alertmanager (always on):"; where "$HOST_PORT_ALERTMANAGER" /; exit 0 ;;
  evidence)     echo "Evidence reports (always on):"; where "$HOST_PORT_EVIDENCE" /; exit 0 ;;
  apisix)
    exec "$ROOT/scripts/admin-ui.sh" ;;
  *) die "usage: make ui UI=prometheus|alertmanager|evidence|apisix" ;;
esac
