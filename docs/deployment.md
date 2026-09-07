# Deployment record

Provisioned on 2026-09-07 in DigitalOcean `nyc1`, using the existing doctl
`freddie-pki` credential. Terraform manages infrastructure only. The Kubernetes
configuration lives in `kubernetes/` and is installed with Helm and kubectl.

## Infrastructure

| Resource | Deployed configuration |
| --- | --- |
| VPC `systems` | `10.70.0.0/20`; ID `62c2020d-0a7d-4d59-b7c6-a6ca8a166b12` |
| NAT `systems-egress` | Default gateway; ID `e628eb78-9eeb-469f-8677-658bb43fb44f`; egress `129.212.198.155` |
| DOKS `systems` | ID `27e1393e-6d47-48b4-8b84-5df941e60fd4`; Kubernetes `1.36.3-do.3`; HA control plane |
| Workers | Two `s-2vcpu-4gb`; `isolated_workers=true`; private addresses `10.70.0.5` and `10.70.0.6`; both Ready |
| Cluster networks | Services `10.71.0.0/20`; pods `10.72.0.0/16` |
| API firewall | Enabled; deployment droplet egress `167.172.3.1/32` and cluster NAT `129.212.198.155/32` |
| PostgreSQL `systems-postgres` | ID `b6634027-f437-49cf-826e-974c84087392`; version 17; one `db-s-1vcpu-2gb` primary |
| Database access | Private endpoint; trusted source restricted to this Kubernetes cluster |
| DOCR | `registry.digitalocean.com/freddierice-systems`, Basic tier, `nyc3`; cluster pull integration enabled |
| Application databases | Separate `health` and `trends` databases and logins on the shared instance |

The control-plane API is public and firewalled. Worker nodes have no public IPs.
Workers initially failed to register until their NAT egress address was allowed
through the API firewall. Terraform now reads the allocated NAT address before
creating/configuring the cluster firewall, so a new deployment includes it.

Local Terraform state and plans are under `infra/`, ignored by Git, and protected
with owner-only permissions. They contain sensitive database and cluster access
data. Preserve this state when moving the deployment runner; do not initialize a
fresh empty state against the existing resources. An encrypted remote state
backend has not been configured.

## Kubernetes bootstrap

Kubeconfig context: `do-nyc1-systems`. Managed Cilium has
`kube-proxy-replacement=true` and `bpf-lb-sock-hostns-only=true`; the preflight
passed without modifying DigitalOcean-managed components.

The database bootstrap completed inside DOKS using the private PostgreSQL endpoint
and TLS hostname/CA verification. Both logins successfully connected to their own
database and were denied access to the other. The `health-database` and
`trends-database` Secrets and public `postgres-ca` ConfigMap are installed in
namespace `systems`. The temporary administrator Secret and Job were removed.

The Google service account `codex-trends@macro-events-882dcb.iam.gserviceaccount.com`
is authenticated on the deployment droplet with the owner-supplied credential.
The two configured Secret Manager versions were successfully read and installed
as `tailscale/operator-oauth`. No Google service-account key is installed in
Kubernetes. See [secret storage](secrets.md).

Tailscale operator `1.102.3` and the systems chart are deployed with Helm. The
operator and both proxy pods are Ready. The staged OAuth client carries all three
systems tags, so the operator uses that exact set for enrollment. After the owner
saved the proxy tag ownership entries, authorization succeeded for each proxy's
individual tag. `tailscale/policy.example.hujson` records the required ownership
and grants; it must be merged into the existing policy.

| Tailnet endpoint | Address |
| --- | --- |
| Application load balancer `systems.impala-hen.ts.net` | `100.91.90.6` |
| Kubernetes API proxy `systems-operator.impala-hen.ts.net` | `100.82.200.29` |

The API proxy passed a TLS-verified request with existing Kubernetes credentials
that listed both nodes. A request from the deployment droplet through the
application load balancer reached Traefik and received the intended HTTPS redirect.
cert-manager registered its ACME account with the private CA through the dedicated DNS resolver and Tailscale egress proxy, verifying the CA root.

The gateway runs Traefik `v3.7.12` from official Helm chart `41.5.0`, with cert-manager `v1.21.1` and Gateway API `v1.6.1`. The `systems` Gateway and four HTTPRoutes replace Traefik's file provider. Its HTTPS listeners reference the cert-manager-owned `health-tls` and `trends-tls` Secrets. The namespaced Issuer uses Gateway API HTTP-01. No DNS-edit credential is installed for certificate issuance.

Traefik runs as non-root UID 65532, with a read-only root filesystem and only `NET_BIND_SERVICE` to listen on the Gateway's standard ports 80/443. It is stateless and has no certificate PVC. The old `systems-caddy-data` PVC remains unmounted for recovery. The Tailscale Service identity and address were preserved.

DOKS initially bundled Gateway API v1.2.1. We used its documented external-install policy to upgrade the standard definitions to v1.6.1, required by Traefik. The pinned installer checks existing versions before applying; DOKS-managed Cilium configuration was not changed.

## Application migration — 2026-09-07

Both apps were migrated from the droplet to DOKS on 2026-09-07. Their final imports committed 45 tables and 978 rows: Health 9 tables / 763 rows, Trends 36 tables / 215 rows. Every table's count and canonical row checksum matched its frozen SQLite snapshot. Health measurements, connected provider tokens/ownership, workouts, and all Trends journal/research/history records were retained. Database sequences were reset after preserving existing IDs.

The original Health checkout contains the owner's uncommitted Measurements and integration work. That content was preserved as commit `fdcbb2b` in an isolated migration worktree before PostgreSQL changes. The live source checkout and its edits were not modified. Both app migration branches are pushed as `codex/postgres-doks`.

| Application | Source commit | DOCR digest |
| --- | --- |
| health | `8ece89719421f79182c16801720f727c8012ee4b` | `sha256:5c56bdd10237fecd1d388e441404c32d70c387568dde5ebdf4ba288780eaea7a` |
| trends | `0ab11d7f21d90a8f287d553c3628b75cdab7ab8a` | `sha256:9a58c69433b52fbd3b36a56a6786588818bfbf303f90bd9f8a9e90bb9a33117e` |

Both application A records now point to `100.91.90.6`, DNS only. cert-manager obtained both certificates from the existing private CA using HTTP-01. TLS-verified readiness checks passed through the Tailscale load balancer. The original droplet services are stopped and disabled; the source, configuration and SQLite files remain for recovery. Frozen snapshots, count/hash reports, unit files and source revisions are retained under `gs://freddie-systems-migration-186933910776/final-2026-09-07/`, with public access prevention and uniform bucket-level access.

All app configuration credentials are in Google Secret Manager; see [secret storage](secrets.md). Trends' workload identity successfully read and added versions to its two API-key secrets, and was denied access to Health's database secret. Both app images connected to the private PostgreSQL endpoint using verified TLS. The migrated containers have no SQLite fallback, no Google administrative key, and no writeable persistent app filesystem. Health OAuth access/refresh state is stored as app data in PostgreSQL.

Background Health sync and Trends provider workers are enabled through `backgroundJobsEnabled: true`. The current production values, including immutable DOCR digests, are committed in `kubernetes/values.production.yaml`. Terraform still manages only DigitalOcean infrastructure; Helm/kubectl manage all Kubernetes configuration.

## Verification

- Both apps' existing SQLite suites passed; the PostgreSQL runs exercised the same app behavior, excluding legacy SQLite schema/file tests. Health: 41 PostgreSQL tests passed, 1 SQLite-only skipped. Trends: 205 passed and 9 SQLite-only skipped in the full PostgreSQL run; its final signal-refresh ordering failure then passed on both backends. Additional PostgreSQL transaction/constraint tests passed, including the new mandatory-fund constraint. The final SQLite run passed 215 tests with the PostgreSQL-only transaction test skipped.
- Consistent import rehearsals and the final imports verified all 45 tables. The final imports used the exact deployed image digests and managed database credentials.
- Both apps' liveness/readiness, main pages, Health Measurements/state/provider routes, Trends funds/trades and provider configuration endpoints returned successful responses.
- The cluster accepted the rendered resources in a server-side dry run. Terraform validation, Helm lint, four mocked Terraform tests and eight Python/Helm tests passed. The final live Terraform plan reported no changes.
- Both certificates were renewed through cert-manager after the Gateway API cutover. CertificateRequests completed successfully, temporary solver routes were removed, and Traefik served the newly issued certificates without a pod restart. HTTPS readiness, application pages, provider configuration and cross-origin write rejection passed through the unchanged Tailscale address.

For subsequent updates, use the [rollout runbook](migration.md). The deployment script defaults to the committed production values. The chart/example defaults keep apps disabled for fresh provisioning. Do not point traffic back at frozen SQLite after PostgreSQL has accepted writes; roll back the image while retaining PostgreSQL or reconcile the data first.
