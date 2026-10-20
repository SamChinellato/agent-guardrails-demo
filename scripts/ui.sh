#!/usr/bin/env bash
# Open one of the secondary UIs while this command runs (Ctrl-C to close).
#   prometheus | alertmanager | evidence   reachable from the LAN (HOST_BIND)
#   apisix                                 this machine only: it is the Admin API port (rule 6)
set -euo pipefail
source "$(dirname "${BASH_SOURCE[0]}")/lib.sh"
ui="${1:-}"
ip="$( (hostname -I 2>/dev/null || ipconfig getifaddr en0 2>/dev/null) | awk '{print $1}')"
where() { echo "  this machine: http://localhost:$1$2"; [[ "$HOST_BIND" == 0.0.0.0 && -n "$ip" ]] && echo "  from the LAN: http://$ip:$1$2"; echo "Ctrl-C to close."; }
case "$ui" in
  prometheus)
    echo "Prometheus (queries, targets, alert rules):"; where 9090 /alerts
    exec kubectl -n observability port-forward --address "$HOST_BIND" svc/kps-prometheus 9090:9090 >/dev/null ;;
  alertmanager)
    echo "Alertmanager (alerts sent to the killswitch):"; where 9093 /
    exec kubectl -n observability port-forward --address "$HOST_BIND" svc/kps-alertmanager 9093:9093 >/dev/null ;;
  evidence)
    echo "Evidence reports (each bundle's report.html):"; where 8099 /
    exec python3 -m http.server 8099 --bind "$HOST_BIND" --directory "$ROOT/evidence" ;;
  apisix)
    exec "$ROOT/scripts/admin-ui.sh" ;;
  *) die "usage: make ui UI=prometheus|alertmanager|evidence|apisix" ;;
esac
