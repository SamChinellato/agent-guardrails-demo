#!/usr/bin/env bash
# Assemble the LokiStack object-storage Secret (loki-s3) from the NooBaa bucket claim's
# generated ConfigMap and Secret. Out of band, like other secrets (OS-09). Idempotent.
set -euo pipefail
source "$(dirname "${BASH_SOURCE[0]}")/../lib.sh"
require_openshift
NS=aigw-observability

wait_for "bucket claim loki" 300 bash -c "[[ \"\$(oc -n $NS get obc loki -o jsonpath='{.status.phase}')\" == Bound ]]"
host="$(oc -n $NS get configmap loki -o jsonpath='{.data.BUCKET_HOST}')"
port="$(oc -n $NS get configmap loki -o jsonpath='{.data.BUCKET_PORT}')"
bucket="$(oc -n $NS get configmap loki -o jsonpath='{.data.BUCKET_NAME}')"
key="$(oc -n $NS get secret loki -o jsonpath='{.data.AWS_ACCESS_KEY_ID}' | base64 -d)"
secret="$(oc -n $NS get secret loki -o jsonpath='{.data.AWS_SECRET_ACCESS_KEY}' | base64 -d)"
oc -n $NS create secret generic loki-s3 \
  --from-literal=bucketnames="$bucket" --from-literal=endpoint="https://$host:$port" \
  --from-literal=access_key_id="$key" --from-literal=access_key_secret="$secret" \
  --from-literal=region=us-east-1 --dry-run=client -o yaml | oc apply -f - >/dev/null
log "loki-s3 storage secret ready (bucket $bucket)"
