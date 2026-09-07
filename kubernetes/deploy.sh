#!/usr/bin/env bash
set -euo pipefail

context=${1:?Usage: kubernetes/deploy.sh KUBE_CONTEXT [VALUES_FILE]}
repo_root=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)
values_file=${2:-"$repo_root/kubernetes/values.example.yaml"}
test -f "$values_file"

# Catch bad app migration/image settings before mutating any cluster resources.
helm template systems "$repo_root/kubernetes/charts/systems" --namespace systems --values "$values_file" >/dev/null
bash "$repo_root/scripts/preflight.sh" "$context"
kubectl --context "$context" -n tailscale get secret operator-oauth -o name >/dev/null

helm upgrade --install tailscale-operator tailscale-operator \
  --repo https://pkgs.tailscale.com/helmcharts --version 1.102.3 \
  --kube-context "$context" --namespace tailscale --create-namespace \
  --values "$repo_root/kubernetes/tailscale-operator.values.yaml" \
  --atomic --wait --timeout 10m

helm upgrade --install systems "$repo_root/kubernetes/charts/systems" \
  --kube-context "$context" --namespace systems --create-namespace \
  --values "$values_file" --atomic --wait --timeout 10m

kubectl --context "$context" -n systems get deployments,services
