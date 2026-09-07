#!/usr/bin/env bash
set -euo pipefail
project=macro-events-882dcb
pool=systems
gcloud services enable iam.googleapis.com sts.googleapis.com secretmanager.googleapis.com --project="$project"
if ! gcloud iam workload-identity-pools describe "$pool" --location=global --project="$project" >/dev/null 2>&1; then
  gcloud iam workload-identity-pools create "$pool" --location=global --project="$project" --display-name="Systems DOKS"
fi
provider=projects/186933910776/locations/global/workloadIdentityPools/systems/providers/doks
jwks=$(mktemp)
trap 'rm -f "$jwks"' EXIT
kubectl get --raw /openid/v1/jwks > "$jwks"
issuer=$(kubectl get --raw /.well-known/openid-configuration | python3 -c 'import json,sys; print(json.load(sys.stdin)["issuer"])')
if gcloud iam workload-identity-pools providers describe doks --workload-identity-pool="$pool" --location=global --project="$project" >/dev/null 2>&1; then
  gcloud iam workload-identity-pools providers update-oidc doks --workload-identity-pool="$pool" --location=global --project="$project" --jwk-json-path="$jwks"
else
  gcloud iam workload-identity-pools providers create-oidc doks --workload-identity-pool="$pool" --location=global --project="$project" --issuer-uri="$issuer" --attribute-mapping=google.subject=assertion.sub --attribute-condition="assertion.sub == 'system:serviceaccount:systems:trends'" --jwk-json-path="$jwks"
fi
principal=principal://iam.googleapis.com/projects/186933910776/locations/global/workloadIdentityPools/systems/subject/system:serviceaccount:systems:trends
for secret in systems-trends-thetadata systems-trends-fmp; do
  for role in roles/secretmanager.secretAccessor roles/secretmanager.secretVersionAdder; do
    gcloud secrets add-iam-policy-binding "$secret" --project="$project" --member="$principal" --role="$role" --condition=None >/dev/null
  done
done
gcloud iam workload-identity-pools create-cred-config "$provider" --credential-source-file=/var/run/google/token --credential-source-type=text --output-file=kubernetes/charts/systems/files/google-credential-config.json
