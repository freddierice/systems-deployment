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
| `charts/systems/` | Traefik gateway, private CA egress, DNS, network policies, and optional applications |

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

The staged OAuth client has Write for **General / Services**, **Devices / Core**,
and **Keys / Auth Keys**, with the tags `tag:systems-operator`, `tag:systems`, and
`tag:systems-ca-egress`. The operator's `defaultTags` match that full set because
Tailscale requires either an exact tag match or ownership of every requested tag.
The app and CA proxies retain their individual tags. Their `tagOwners` entries
must name `tag:systems-operator`; selecting tags on the OAuth client does not
establish tag ownership in the tailnet policy. Merge
`tailscale/policy.example.hujson` into the existing policy before installing.

When rotating the client, retain its tag set or update `operatorConfig.defaultTags`
to match the new client. A client carrying only `tag:systems-operator` can instead
use that single tag for operator enrollment, with the same proxy ownership entries.
See [Tailscale's tag rules](https://tailscale.com/docs/features/tags).

Applications require `postgresMigrationVerified: true` and immutable image digests
before their Deployments can render. See `docs/migration.md` for the required
PostgreSQL code and data changes.
