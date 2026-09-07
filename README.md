# systems-deployment

Terraform for one DigitalOcean Kubernetes cluster named **systems**, with private-only worker IPs, one managed PostgreSQL primary shared by Health and Trends, and a Tailscale load balancer serving **health.freddie.xyz** and **trends.freddie.xyz**.

**Status: deployment repository prepared; nothing provisioned or cut over.** Both applications remain on the existing droplet with SQLite. Application deployment is disabled by default and guarded by an explicit PostgreSQL migration flag. See [the application contract and migration checklist](docs/migration.md).

```mermaid
flowchart LR
    U[Authorized Tailscale clients] --> L[Tailscale LoadBalancer: systems]
    L --> C[Caddy: HTTPS for app subdomains]
    C --> H[health: ClusterIP]
    C --> T[trends: ClusterIP]
    H --> P[(systems-postgres: health database)]
    T --> P2[(Same instance: trends database)]
    C --> E[Tailscale CA egress proxy]
    E --> CA[Existing ca.freddie.xyz]
    CA -->|HTTP-01 on port 80| L
    N[Private DOKS workers] --> NAT[VPC NAT gateway: outbound internet]
```

## What Terraform manages

| Root | Resources |
| --- | --- |
| `infra/` | `systems` VPC in `nyc1`, default NAT gateway, DOKS with isolated workers and a control-plane firewall, one PostgreSQL primary, two databases and logins, database trusted-source firewall |
| `platform/` | Tailscale Operator, local `systems` Helm chart, Caddy and persistent certificate storage, private CA routing, gateway-specific DNS resolver, network policies, optional app Deployments |

The roots have independent state. Create infrastructure and obtain a working kubeconfig before initializing Kubernetes resources; there is no one-pass provider/cluster bootstrap dependency. Application images and database migrations belong in their application repositories. CI validates configuration with mocked providers and never applies infrastructure.

Default capacity is two `s-2vcpu-4gb` workers and one `db-s-1vcpu-2gb` PostgreSQL primary, with no database standby. Caddy, each app, and each standalone Tailscale proxy run one replica. Updates and failover can interrupt requests; two workers do not make every component highly available. App replicas stay at one because the apps currently run background work inside their processes.

## Networking and HTTPS

- Workers use `isolated_workers = true`, available for new DOKS 1.36+ clusters in public preview. A default DigitalOcean VPC NAT gateway supplies outbound traffic for provisioning, image pulls, Tailscale, and external APIs. A VPC by itself does not remove public worker IPs. [DOKS isolated workers](https://docs.digitalocean.com/products/kubernetes/how-to/create-clusters-with-isolated-worker-nodes/)
- The DOKS API endpoint remains public with a restrictive firewall. `admin_cidrs` must contain the administrator/runner's **public egress** address, not its `100.x` Tailscale address. The operator also exposes an API proxy as `systems-operator` using `noauth` mode: it passes through Kubernetes credentials and RBAC, not anonymous cluster access. The example tailnet grant limits it to the owner. [API server proxy](https://tailscale.com/docs/kubernetes-operator/api-server-proxy)
- The only application `LoadBalancer` uses `loadBalancerClass: tailscale`, with NodePort allocation disabled. There is no public DigitalOcean load balancer or Tailscale Funnel. Network policies permit the Tailscale namespace to reach Caddy, and Caddy to reach app ports. App egress remains available for external integrations.
- VPC `10.70.0.0/20`, services `10.71.0.0/20`, pods `10.72.0.0/16`. Confirm these do not overlap existing VPCs or advertised tailnet routes before creation. The gateway's private DNS service reserves `10.71.0.53`.
- PostgreSQL uses its **private hostname**, a Kubernetes trusted-source firewall rule, and `sslmode=verify-full` with the DigitalOcean database CA. The managed service may still have a public hostname; this repository does not publish it to applications or allow world access. App-level database isolation is established and tested by the bootstrap job, not merely by creating two logins.

Existing private PKI is retained: Caddy gets certificates from `https://ca.freddie.xyz/acme/acme/directory`, using HTTP-01 and the public root certificate in `charts/systems/files/root_ca.crt`. Root SHA-256: `53de014733269d464ed65fac577936986355e2a55cf3d0ae81623aacf8daeab4`. No CA signing key is copied or required.

Caddy needs to reach the CA over Tailscale. An egress Service targets `ca-nyc1.impala-hen.ts.net`. A dedicated DNS resolver rewrites **only** `ca.freddie.xyz` for the gateway to this Service; Caddy still validates the certificate against `ca.freddie.xyz`. DOKS-managed CoreDNS is unchanged. The CA must also be allowed to connect back to the new load balancer on TCP 80 to validate and renew certificates. [Tailscale egress](https://tailscale.com/docs/kubernetes-operator/egress/access-tailnet-service)

## Validate locally

Requires Terraform 1.16.1 (minimum supported by configuration: 1.11), Helm 3.19.0, Python 3.12+, and Make. Applying also requires `doctl`, `kubectl` matching the cluster version, DigitalOcean credentials, and Tailscale operator credentials.

```sh
python3 -m venv .venv
. .venv/bin/activate
pip install -r requirements-dev.txt
make init check
```

Provider locks are committed. Tests exercise private-cluster settings, firewall validation, image/migration gates, rendered service exposure, probes, and secret URL handling without cloud credentials. They do not prove live DOKS availability, SQL permissions, Cilium compatibility, or CA renewal.

## Provisioning runbook

These are future deployment commands, not actions taken when this repository was prepared.

1. Confirm region, worker/database sizes, Kubernetes versions, NAT availability, private-worker preview availability, and non-overlapping networks in the DigitalOcean account. Check current billing for workers, NAT, database, persistent volume, and any control-plane charges. [Kubernetes pricing](https://docs.digitalocean.com/products/kubernetes/details/pricing/) · [NAT pricing](https://docs.digitalocean.com/products/networking/vpc/details/pricing/)

2. Select where state will live before applying. The roots default to local state for a single administrator; use `umask 077`, owner-only storage and encrypted backups. State and saved plans contain database passwords and DOKS credentials even when outputs are marked sensitive. For collaboration, configure an encrypted remote backend with locking, using separate keys for `infra` and `platform`, and migrate existing state. Never commit state, credentials, local tfvars or plan files.

3. Configure the DigitalOcean token locally and replace the example administrator CIDR:

   ```sh
   umask 077
   read -rs -p 'DigitalOcean token: ' DIGITALOCEAN_TOKEN
   export DIGITALOCEAN_TOKEN
   cp infra/terraform.tfvars.example infra/terraform.tfvars
   # Edit infra/terraform.tfvars with real account settings.
   terraform -chdir=infra init
   terraform -chdir=infra plan -out=systems.tfplan
   terraform -chdir=infra apply systems.tfplan
   doctl kubernetes cluster kubeconfig save systems
   ```

   VPC, cluster, database instance, and app databases have `prevent_destroy`. Replacements require deliberate code changes and a backup/restore plan. Avoid broad `-target`/destroy operations.

4. Verify the cluster and networking before installing Tailscale:

   ```sh
   doctl kubernetes cluster get systems --output json
   bash scripts/preflight.sh do-nyc1-systems
   ```

   Check `isolated_workers=true` in the DigitalOcean response as well as the worker addresses. The preflight fails if Cilium's kube-proxy replacement cannot be verified compatible with Tailscale L4 Service proxies. Tailscale requires socket LB bypass in pod namespaces; DigitalOcean manages Cilium and warns against patching it. Obtain a supported configuration from DigitalOcean if this check fails. [Tailscale Cilium requirements](https://tailscale.com/docs/features/kubernetes-operator) · [DOKS managed components](https://docs.digitalocean.com/products/kubernetes/details/managed/)

5. Merge `tailscale/policy.example.hujson` into the existing tailnet policy, replacing the owner login placeholder and verifying the CA's current tailnet address. Existing broad grants are additive: they must also be reviewed to maintain owner-only application access. Create an OAuth client tagged `tag:systems-operator` following the [operator install guide](https://tailscale.com/docs/kubernetes-operator/install-operator), with the required Devices/Core, Auth Keys, and Services write scopes. Enable tailnet HTTPS for the API proxy if it is not already enabled. If device approval or Tailnet Lock is enabled, approve/sign the operator and its proxies as required.

   ```sh
   read -r -p 'Tailscale OAuth client ID: ' TS_OAUTH_CLIENT_ID
   read -rs -p 'Tailscale OAuth client secret: ' TS_OAUTH_CLIENT_SECRET
   export TS_OAUTH_CLIENT_ID TS_OAUTH_CLIENT_SECRET
   bash scripts/bootstrap-operator.sh do-nyc1-systems
   unset TS_OAUTH_CLIENT_ID TS_OAUTH_CLIENT_SECRET
   cp platform/terraform.tfvars.example platform/terraform.tfvars
   terraform -chdir=platform init
   terraform -chdir=platform plan -out=systems.tfplan
   terraform -chdir=platform apply systems.tfplan
   ```

   Keep `apps = {}` and `postgres_migration_verified = false`. The gateway serves preparation responses and certificate issuance may retry until CA validation DNS targets it. This does not change existing DNS or the droplet.

6. Initialize database grants and Kubernetes Secrets:

   ```sh
   python3 scripts/bootstrap-database.py --context do-nyc1-systems
   ```

   The script reads sensitive Terraform outputs through a pipe, runs an ephemeral PostgreSQL client Job in the cluster, verifies each login can connect to its own database and cannot connect to the other, and then writes `health-database` and `trends-database` Secrets. Database access uses the private endpoint with certificate verification. The admin Secret and Job are removed on completion or failure. A forced process termination may require manually deleting resources named `database-bootstrap-*`. Rerun after a password/CA rotation and restart the affected app pods; environment variables do not update in existing pods.

7. Complete the [app migration and cutover checklist](docs/migration.md). Supply immutable app images and registry pull Secrets. Move per-app integration settings into optional `health-runtime`/`trends-runtime` Kubernetes Secrets, using a secret manager or protected local env files. Do not copy SQLite paths, loopback-only sidecar URLs, or droplet service settings unchanged. App PostgreSQL credentials come only from their dedicated database Secrets.

## DNS and certificate cutover

DNS is intentionally a separate cutover step because the current Cloudflare records route to the live SQLite deployment. Keep **DNS only** (no Cloudflare proxy). Obtain the new Tailscale IPv4 from the `systems` device in the Tailscale admin console or from Service status if populated:

```sh
kubectl --context do-nyc1-systems -n systems get service systems-gateway -o json
```

The status may report a MagicDNS hostname; resolve it from a tailnet client to get its `100.x` address. At cutover, replace the existing `100.66.233.125` A-record targets for **health** and **trends** with that address. Check for stale AAAA records. The apex `freddie.xyz` and CA DNS are unaffected. Devices still need Tailscale access and the Freddie root installed.

HTTP-01 is a two-way dependency: Caddy must reach the CA, and the CA's DNS must resolve each application hostname to the new gateway. A client-side `curl --resolve` alone does not satisfy CA validation. Before cutover, use a deliberate temporary DNS override on the CA resolver to issue and test certificates while normal users stay on the droplet; remove it once DNS is switched. Otherwise budget an issuance window during cutover. Verify certificate renewal across the existing short certificate lifetime, including after a gateway restart. Changing/deleting a Tailscale proxy's Kubernetes identity can change its tailnet IP and require a DNS update.

The Caddy PVC retains certificate/account state (`helm.sh/resource-policy: keep`). Preserve it across chart uninstall/reinstall; do not run multiple independent Caddy replicas against the same single-writer certificate store. Keep and back up operator-managed Secrets too, because they contain Tailscale device identity. A rollback after PostgreSQL has accepted new writes requires data reconciliation; pointing DNS back to the stale SQLite files would lose access to those writes.
