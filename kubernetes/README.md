# Kubernetes configuration

This directory is deployed with Helm and kubectl, independently of Terraform.
Terraform creates only the DigitalOcean infrastructure in `../infra`.

| File | Purpose |
| --- | --- |
| `deploy.sh` | Validate values and cluster networking, then install Gateway API, the operator, cert-manager, Traefik and the systems chart |
| `tailscale-operator.values.yaml` | Operator settings; OAuth credentials stay in an existing Secret |
| `install-gateway-api.py` | Checksum-pinned standard CRDs; DOKS external ownership and downgrade guards |
| `traefik.values.yaml`, `cert-manager.values.yaml` | Controller configuration; Gateway API only and HTTP-01 support |
| `values.production.yaml` | Live DOCR digests, app enablement and workload identity |
| `values.example.yaml` | Deployment settings with apps disabled pending PostgreSQL migration |
| `google-secrets.yaml` | Google Secret Manager resource mappings for operator and app credentials |
| `bootstrap-google-secrets.py` | Read latest versions through gcloud and synchronize Kubernetes Secrets |
| `charts/systems/` | Gateway/HTTPRoutes, Issuer/Certificates, Tailscale Services, private DNS, network policies and apps |

After saving the cluster kubeconfig and signing into Google with Secret Accessor
permission on the two configured secrets:

```sh
python3 kubernetes/bootstrap-google-secrets.py --context do-nyc1-systems --operator-only
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

For the live deployment, synchronize all secrets without `--operator-only` and run `bash kubernetes/deploy.sh do-nyc1-systems`; it defaults to production values. cert-manager writes TLS Secrets in `systems`, beside their consuming Gateway. App pods use plain HTTP behind Traefik; PostgreSQL TLS uses its separate DigitalOcean CA. Certificate issuance and renewal need the CA's DNS to resolve both app hostnames to the Tailscale gateway.

Check `kubectl -n systems get gateway,httproute,issuer,certificate` after deployment. On initial provisioning, the Gateway can be pending until cert-manager issues its first certificates. The script reports this as an unfinished deployment; correct CA/DNS access and rerun. Renewals update Secrets and are loaded by Traefik automatically. Keep the DNS service IP in the chart and cert-manager values aligned if changing the service range.
