# Kubernetes configuration

This directory is deployed with Helm and kubectl, independently of Terraform.
Terraform creates only the DigitalOcean infrastructure in `../infra`.

| File | Purpose |
| --- | --- |
| `deploy.sh` | Validate values and cluster networking, then install/upgrade the operator and systems chart |
| `tailscale-operator.values.yaml` | Operator settings; OAuth credentials stay in an existing Secret |
| `values.example.yaml` | Deployment settings with apps disabled pending PostgreSQL migration |
| `google-secrets.yaml` | The two Google Secret Manager resource names for operator credentials |
| `bootstrap-google-secrets.py` | Read those versions through gcloud and install the operator Secret |
| `charts/systems/` | Gateway, private CA egress, DNS, network policies, and optional applications |

After saving the cluster kubeconfig and signing into Google with Secret Accessor
permission on the two configured secrets:

```sh
python3 kubernetes/bootstrap-google-secrets.py --context do-nyc1-systems
cp kubernetes/values.example.yaml kubernetes/values.local.yaml
helm template systems kubernetes/charts/systems \
  --namespace systems --values kubernetes/values.local.yaml
bash kubernetes/deploy.sh do-nyc1-systems kubernetes/values.local.yaml
```

`values.local.yaml` is ignored. Keep secret values out of Helm values: Helm stores
release values in cluster Secrets. The chart references per-app database/runtime
Secrets instead. Use `scripts/bootstrap-operator.sh` and
`scripts/bootstrap-database.py` for the initial bootstrap.

The Google bootstrap reads the latest versions on demand. After OAuth credential
rotation, rerun it and restart the `tailscale/operator` Deployment. Continuous
Secret Manager synchronization through External Secrets and workload identity is
a later integration. The initial bootstrap uses the owner-supplied `codex-trends`
service-account credential on the deployment droplet; no Google private key is
installed in the cluster. See `docs/secrets.md` for its location and scope.

For the operator OAuth client, select Write for **General / Services**,
**Devices / Core**, and **Keys / Auth Keys**, each restricted to
`tag:systems-operator`. Define the tags and grants from
`tailscale/policy.example.hujson` in the existing tailnet policy before installing.

Applications require `postgresMigrationVerified: true` and immutable image digests
before their Deployments can render. See `docs/migration.md` for the required
PostgreSQL code and data changes.
