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
application load balancer reached Caddy and received the intended HTTPS redirect.
Caddy reached the private CA's ACME directory through its dedicated DNS resolver
and Tailscale egress proxy, with TLS hostname and root verification.

The gateway and its two dedicated DNS pods were verified Ready. The chart uses an
explicit Caddy executable and retains `NET_BIND_SERVICE` in the capability
bounding set because both upstream images attach that file capability to their
binaries. The containers still run as non-root with a read-only root filesystem.

## Validation and remaining cutover

- Terraform configuration, Helm lint, shell/Python syntax, four mocked Terraform
  checks, and seven Python/Helm checks passed.
- The final live Terraform plan reported no changes.
- DigitalOcean reports the cluster running with isolated workers and the intended
  API firewall. The database has only the cluster trusted-source rule.
- Live database grants, private TLS connectivity, and cross-database isolation
  passed.
- Caddy's local health endpoint returned `ok`; HTTP GET returned a 308 HTTPS
  redirect and HTTP POST returned 403.
- Tailscale load-balancer routing, the authenticated Kubernetes API proxy, and
  private CA connectivity passed. Application certificate issuance and renewal
  remain unverified: the app hostnames still resolve to the live droplet, so
  HTTP-01 validation does not target this gateway. Follow the README's certificate
  and DNS cutover procedure during migration; Caddy may retry issuance meanwhile.
- Both application Deployments remain disabled. No SQLite data has been migrated,
  no application code has been changed, and no application DNS has been switched.
  The live droplet deployment remains the current service.

For subsequent platform updates:

```sh
# After replacing the OAuth credential in Secret Manager:
python3 kubernetes/bootstrap-google-secrets.py --context do-nyc1-systems
kubectl --context do-nyc1-systems -n tailscale rollout restart deployment/operator
# Install/upgrade the Kubernetes configuration:
bash kubernetes/deploy.sh do-nyc1-systems
```

Complete [the application migration](migration.md) and the root README's DNS and
certificate cutover sequence before enabling application Deployments or changing
the existing `health.freddie.xyz` and `trends.freddie.xyz` records.
