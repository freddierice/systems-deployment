#!/usr/bin/env bash
set -euo pipefail

context=${1:?Usage: kubernetes/deploy.sh KUBE_CONTEXT [VALUES_FILE]}
repo_root=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)
values_file=${2:-"$repo_root/kubernetes/values.production.yaml"}
test -f "$values_file"

# Catch bad app migration/image settings before mutating any cluster resources.
helm template systems "$repo_root/kubernetes/charts/systems" --namespace systems --values "$values_file" >/dev/null
bash "$repo_root/scripts/preflight.sh" "$context"
kubectl --context "$context" -n tailscale get secret operator-oauth -o name >/dev/null
python3 "$repo_root/kubernetes/install-gateway-api.py" --context "$context"

helm upgrade --install tailscale-operator tailscale-operator \
  --repo https://pkgs.tailscale.com/helmcharts --version 1.102.3 \
  --kube-context "$context" --namespace tailscale --create-namespace \
  --values "$repo_root/kubernetes/tailscale-operator.values.yaml" \
  --atomic --wait --timeout 10m

helm upgrade --install cert-manager cert-manager \
  --repo https://charts.jetstack.io --version v1.21.1 \
  --kube-context "$context" --namespace cert-manager --create-namespace \
  --values "$repo_root/kubernetes/cert-manager.values.yaml" \
  --atomic --wait --timeout 10m

helm upgrade --install traefik traefik \
  --repo https://traefik.github.io/charts --version 41.5.0 --skip-crds \
  --kube-context "$context" --namespace systems --create-namespace \
  --values "$repo_root/kubernetes/traefik.values.yaml" \
  --atomic --wait --timeout 10m

helm upgrade --install systems "$repo_root/kubernetes/charts/systems" \
  --kube-context "$context" --namespace systems --create-namespace \
  --values "$values_file" --atomic --wait --timeout 10m

kubectl --context "$context" -n systems get deployments,services
kubectl --context "$context" -n systems wait gateway/systems --for=condition=Programmed --timeout=60s
kubectl --context "$context" -n systems get issuer,certificate,httproute
# Initial certificates may remain pending until the CA resolves app DNS to this
# gateway. cert-manager retries automatically; inspect Certificate/Challenge status.
