# systems-deployment

Terraform for one DigitalOcean Kubernetes cluster named **systems**, with private-only worker IPs, one managed PostgreSQL primary shared by Health and Trends, and a Tailscale load balancer serving **health.freddie.xyz** and **trends.freddie.xyz**.

**Status: infrastructure deployment is tracked in [the deployment record](docs/deployment.md).** Both applications remain on the existing droplet with SQLite. Application deployment is disabled by default and guarded by an explicit PostgreSQL migration flag. See [the application contract and migration checklist](docs/migration.md).

```mermaid
flowchart LR
    U[Authorized Tailscale clients] --> L[Tailscale LoadBalancer: systems]
    L --> C[Traefik: HTTPS for app subdomains]
    C --> H[health: ClusterIP]
    C --> T[trends: ClusterIP]
    H --> P[(systems-postgres: health database)]
    T --> P2[(Same instance: trends database)]
    C --> E[Tailscale CA egress proxy]
    E --> CA[Existing ca.freddie.xyz]
    CA -->|HTTP-01 on port 80| L
    N[Private DOKS workers] --> NAT[VPC NAT gateway: outbound internet]
```

## Deployment layout

| Directory | Resources |
| --- | --- |
| `infra/` | `systems` VPC in `nyc1`, default NAT gateway, DOKS with isolated workers and a control-plane firewall, one PostgreSQL primary, two databases and logins, database trusted-source firewall |
| `kubernetes/` (Helm directly) | Tailscale Operator, local `systems` Helm chart, Traefik and persistent certificate storage, private CA routing, gateway-specific DNS resolver, network policies, optional app Deployments |

Terraform manages only DigitalOcean resources in `infra/`. Kubernetes resources are installed separately by `kubernetes/deploy.sh` using Helm; there are no Helm or Kubernetes Terraform providers or resources. Create infrastructure and obtain a working kubeconfig before deploying Kubernetes configuration. Application images and database migrations belong in their application repositories. CI validates configuration with mocked providers and never applies infrastructure.

Default capacity is two `s-2vcpu-4gb` workers and one `db-s-1vcpu-2gb` PostgreSQL primary, with no database standby. Traefik, each app, and each standalone Tailscale proxy run one replica. Updates and failover can interrupt requests; two workers do not make every component highly available. App replicas stay at one because the apps currently run background work inside their processes.

## Networking and HTTPS

- Workers use `isolated_workers = true`, available for new DOKS 1.36+ clusters in public preview. A default DigitalOcean VPC NAT gateway supplies outbound traffic for provisioning, image pulls, Tailscale, and external APIs. A VPC by itself does not remove public worker IPs. [DOKS isolated workers](https://docs.digitalocean.com/products/kubernetes/how-to/create-clusters-with-isolated-worker-nodes/)
- The DOKS API endpoint remains public with a restrictive firewall. `admin_cidrs` must contain the administrator/runner's **public egress** address, not its `100.x` Tailscale address. Terraform also allows the cluster NAT's allocated IPv4 address so isolated workers can bootstrap. The operator exposes an API proxy as `systems-operator` using `noauth` mode: it passes through Kubernetes credentials and RBAC, not anonymous cluster access. The example tailnet grant limits it to the owner. [API server proxy](https://tailscale.com/docs/kubernetes-operator/api-server-proxy)
- The only application `LoadBalancer` uses `loadBalancerClass: tailscale`, with NodePort allocation disabled. There is no public DigitalOcean load balancer or Tailscale Funnel. Network policies permit the Tailscale namespace to reach Traefik, and Traefik to reach app ports. App egress remains available for external integrations.
- VPC `10.70.0.0/20`, services `10.71.0.0/20`, pods `10.72.0.0/16`. Confirm these do not overlap existing VPCs or advertised tailnet routes before creation. The gateway's private DNS service reserves `10.71.0.53`.
- PostgreSQL uses its **private hostname**, a Kubernetes trusted-source firewall rule, and `sslmode=verify-full` with the DigitalOcean database CA. The managed service may still have a public hostname; this repository does not publish it to applications or allow world access. App-level database isolation is established and tested by the bootstrap job, not merely by creating two logins.

Existing private PKI is retained: Traefik gets certificates from `https://ca.freddie.xyz/acme/acme/directory`, using HTTP-01 and the public root certificate in `kubernetes/charts/systems/files/root_ca.crt`. Root SHA-256: `53de014733269d464ed65fac577936986355e2a55cf3d0ae81623aacf8daeab4`. No CA signing key is copied or required.

Traefik `v3.7.12` uses the file provider: Helm renders `traefik.yaml` and
`routes.yaml` into the gateway ConfigMap. GET/HEAD on HTTP receive a permanent
HTTPS redirect; other HTTP app requests have no matching router and receive
404. The internal ACME challenge route remains available on port 80. Disabled
apps have empty backend pools and return 503 once a valid TLS certificate is
available. Strict SNI rejects TLS connections without a matching certificate.
There is no exposed dashboard or gateway Kubernetes API credential.

`privateCA.certificatesDurationHours: 24` aligns the renewal schedule with the
existing CA's approximately 24-hour leaf certificates. Adjust it if the CA's
issuance policy changes. [Traefik ACME configuration](https://doc.traefik.io/traefik/reference/install-configuration/tls/certificate-resolvers/acme/)

Traefik needs to reach the CA over Tailscale. An egress Service targets `ca-nyc1.impala-hen.ts.net`. A dedicated DNS resolver rewrites **only** `ca.freddie.xyz` for the gateway to this Service; Traefik still validates the certificate against `ca.freddie.xyz`. DOKS-managed CoreDNS is unchanged. The CA must also be allowed to connect back to the new load balancer on TCP 80 to validate and renew certificates. [Tailscale egress](https://tailscale.com/docs/kubernetes-operator/egress/access-tailnet-service)

## Validate locally

Requires Terraform 1.16.1 (minimum supported by configuration: 1.11), Helm 3.19.0, Python 3.12+, and Make. Applying also requires `doctl`, `kubectl` matching the cluster version, DigitalOcean credentials, and Tailscale operator credentials. `scripts/with-doctl.py` also uses PyYAML from `requirements-dev.txt`.

```sh
python3 -m venv .venv
. .venv/bin/activate
pip install -r requirements-dev.txt
make init check
```

Provider locks are committed. Tests exercise private-cluster settings, firewall validation, image/migration gates, rendered service exposure, probes, and secret URL handling without cloud credentials. They do not prove live DOKS availability, SQL permissions, Cilium compatibility, or CA renewal.

## Provisioning runbook

Use these commands to reproduce or update the deployment. See the deployment record for what has actually been applied.

1. Confirm region, worker/database sizes, Kubernetes versions, NAT availability, private-worker preview availability, and non-overlapping networks in the DigitalOcean account. Check current billing for workers, NAT, database, persistent volume, and any control-plane charges. [Kubernetes pricing](https://docs.digitalocean.com/products/kubernetes/details/pricing/) · [NAT pricing](https://docs.digitalocean.com/products/networking/vpc/details/pricing/)

2. Select where state will live before applying. The infrastructure root uses local state for a single administrator; use `umask 077`, owner-only storage and encrypted backups. State and saved plans can contain database passwords and DOKS credentials even when outputs are marked sensitive. For collaboration, configure an encrypted remote backend with locking, and migrate existing infrastructure state. Helm stores release state in the cluster. Never commit state, credentials, local tfvars or plan files.

3. Use the existing credential in doctl’s `freddie-pki` context and replace the example administrator CIDR. The wrapper reads that token into the child environment without printing it or saving it in Terraform configuration:

   ```sh
   umask 077
   cp infra/terraform.tfvars.example infra/terraform.tfvars
   # Edit infra/terraform.tfvars with real account settings.
   terraform -chdir=infra init
   python3 scripts/with-doctl.py --context freddie-pki terraform -chdir=infra plan -out=systems.tfplan
   python3 scripts/with-doctl.py --context freddie-pki terraform -chdir=infra apply systems.tfplan
   doctl --context freddie-pki kubernetes cluster kubeconfig save systems
   ```

   VPC, cluster, database instance, and app databases have `prevent_destroy`. Replacements require deliberate code changes and a backup/restore plan. Avoid broad `-target`/destroy operations.

4. Verify the cluster and networking before installing Tailscale:

   ```sh
   doctl --context freddie-pki kubernetes cluster get systems --output json
   bash scripts/preflight.sh do-nyc1-systems
   ```

   Check `isolated_workers=true` in the DigitalOcean response as well as the worker addresses. The preflight fails if Cilium's kube-proxy replacement cannot be verified compatible with Tailscale L4 Service proxies. Tailscale requires socket LB bypass in pod namespaces; DigitalOcean manages Cilium and warns against patching it. Obtain a supported configuration from DigitalOcean if this check fails. [Tailscale Cilium requirements](https://tailscale.com/docs/features/kubernetes-operator) · [DOKS managed components](https://docs.digitalocean.com/products/kubernetes/details/managed/)

5. Merge `tailscale/policy.example.hujson` into the existing tailnet policy, replacing the owner login placeholder and verifying the CA's current tailnet address. Existing broad grants are additive: they must also be reviewed to maintain owner-only application access. The operator credentials are stored under the two resource names in `kubernetes/google-secrets.yaml` (project `186933910776`). The staged OAuth client has Devices/Core, Auth Keys, and Services write scopes with all three systems tags; `operatorConfig.defaultTags` matches that set. The proxy tags must be owned by `tag:systems-operator` in the tailnet policy. See [Kubernetes configuration](kubernetes/README.md) for tag matching and rotation, and the [operator install guide](https://tailscale.com/docs/kubernetes-operator/install-operator). Enable tailnet HTTPS for the API proxy if it is not already enabled. If device approval or Tailnet Lock is enabled, approve/sign the operator and its proxies as required.

   ```sh
   # Initial deployment: use the credential supplied on the deployment droplet.
   gcloud auth activate-service-account --key-file=/home/codex/.config/gcloud/codex-trends.json
   python3 kubernetes/bootstrap-google-secrets.py --context do-nyc1-systems
   cp kubernetes/values.example.yaml kubernetes/values.local.yaml
   bash kubernetes/deploy.sh do-nyc1-systems kubernetes/values.local.yaml
   ```

   Keep both `apps.*.enabled` flags and `postgresMigrationVerified` set to `false`. Once Tailscale credentials are available, the gateway serves preparation responses and certificate issuance may retry until CA validation DNS targets it. This does not change existing DNS or the droplet.

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

HTTP-01 is a two-way dependency: Traefik must reach the CA, and the CA's DNS must resolve each application hostname to the new gateway. A client-side `curl --resolve` alone does not satisfy CA validation. Before cutover, use a deliberate temporary DNS override on the CA resolver to issue and test certificates while normal users stay on the droplet; remove it once DNS is switched. Otherwise budget an issuance window during cutover. Verify certificate renewal across the existing short certificate lifetime, including after a gateway restart. Changing/deleting a Tailscale proxy's Kubernetes identity can change its tailnet IP and require a DNS update.

The gateway PVC retains certificate/account state (`helm.sh/resource-policy: keep`). Its historical name, `systems-caddy-data`, is retained to reuse the existing volume. Traefik stores its own state in `/data/traefik-acme.json`; previous Caddy files remain available for rollback and are not consumed by Traefik. Preserve the volume across chart uninstall/reinstall; do not run multiple independent Traefik replicas against the same single-writer certificate store. Keep and back up operator-managed Secrets too, because they contain Tailscale device identity. A rollback after PostgreSQL has accepted new writes requires data reconciliation; pointing DNS back to the stale SQLite files would lose access to those writes.
