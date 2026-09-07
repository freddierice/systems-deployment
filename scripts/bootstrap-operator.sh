#!/usr/bin/env bash
set -euo pipefail

context=${1:?Usage: bootstrap-operator.sh KUBE_CONTEXT}
: "${TS_OAUTH_CLIENT_ID:?Set TS_OAUTH_CLIENT_ID locally}"
: "${TS_OAUTH_CLIENT_SECRET:?Set TS_OAUTH_CLIENT_SECRET locally}"

kubectl --context "$context" create namespace tailscale --dry-run=client -o yaml |
  kubectl --context "$context" apply -f -

# Credentials are streamed on stdin, never CLI arguments or files.
python3 - <<'PY' | kubectl --context "$context" apply --server-side --field-manager=systems-bootstrap -f -
import base64
import json
import os
print(json.dumps({
    "apiVersion": "v1", "kind": "Secret",
    "metadata": {"name": "operator-oauth", "namespace": "tailscale"},
    "type": "Opaque",
    "data": {
        "client_id": base64.b64encode(os.environ["TS_OAUTH_CLIENT_ID"].encode()).decode(),
        "client_secret": base64.b64encode(os.environ["TS_OAUTH_CLIENT_SECRET"].encode()).decode(),
    },
}))
PY

printf '%s\n' 'Operator credentials installed. Restart the operator after a credential rotation.'
