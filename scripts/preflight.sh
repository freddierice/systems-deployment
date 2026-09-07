#!/usr/bin/env bash
set -euo pipefail
context=${1:?Usage: preflight.sh KUBE_CONTEXT}

kubectl --context "$context" get nodes -o json | python3 -c '
import json,sys
nodes=json.load(sys.stdin)["items"]
assert nodes, "No nodes found"
for node in nodes:
    public=[a["address"] for a in node["status"].get("addresses",[]) if a["type"]=="ExternalIP"]
    name=node["metadata"]["name"]
    assert not public, f"Worker {name} has ExternalIP addresses: {public}"
print("All workers have no Kubernetes ExternalIP addresses; also verify isolated_workers=true with doctl.")
'

# Tailscale L4 Service proxies need socket load balancing bypassed in pod
# namespaces with Cilium kube-proxy replacement. Do not patch managed Cilium.
kubectl --context "$context" -n kube-system get configmap cilium-config -o json | python3 -c '
import json,sys
data=json.load(sys.stdin)["data"]
replacement=data.get("kube-proxy-replacement", "unknown").lower()
host_only=data.get("bpf-lb-sock-hostns-only", "false").lower()
if replacement not in ("false", "disabled") and host_only != "true":
    sys.exit("Cilium socket-LB compatibility is not verified. Ask DigitalOcean for the supported host-namespace-only setting before deploying Tailscale LoadBalancer Services.")
print("Cilium socket-LB configuration is compatible with Tailscale Service proxies.")
'
