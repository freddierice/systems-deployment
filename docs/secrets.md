# Secret storage

Google Secret Manager project `186933910776` (`macro-events-882dcb`) is the central store for deployment and integration credentials.

| Secret | Consumer |
| --- | --- |
| `systems-cluster-tailscale-id`, `systems-cluster-tailscale-secret` | Tailscale operator bootstrap |
| `systems-health-database`, `systems-trends-database` | JSON `DATABASE_URL`, synchronized to each app's Kubernetes Secret |
| `systems-postgres-bootstrap` | Administrative database bootstrap and credential recovery; never mounted in app pods |
| `systems-health-runtime` | JSON Health OAuth client settings, synchronized to `health-runtime` |
| `systems-trends-thetadata`, `systems-trends-fmp` | Trends settings UI and background data providers |
| `systems-digitalocean-token` | Deployment credential recovery; doctl remains the runner source |
| `systems-cluster-cloudflare-account`, `systems-cluster-cloudflare-token` | The deployment runner's app DNS script |

`kubernetes/bootstrap-google-secrets.py --context do-nyc1-systems` reads the mapping in `kubernetes/google-secrets.yaml`. It streams payloads through subprocess input without printing them or passing them in command-line arguments. Restart affected app deployments after changing environment-based secrets. Restart the operator after changing its OAuth credential.

`./scripts/publish-database-secrets.py` publishes database credentials from protected Terraform state. Run it after provisioning or rotating DO database credentials, then synchronize Kubernetes Secrets and restart the affected app. `./scripts/import-runtime-secrets.py --health /path/to/health --trends /path/to/trends` is a **one-time** legacy import: do not rerun it after settings have changed in Google.

Trends uses Workload Identity Federation directly. The `systems` pool's `doks` provider accepts only `system:serviceaccount:systems:trends` from the cluster's uploaded JWKS. That principal has `secretAccessor` and `secretVersionAdder` only on the two Trends API-key secrets. A projected, hourly Kubernetes token is exchanged for short-lived Google credentials. `kubernetes/charts/systems/files/google-credential-config.json` is public configuration, not a private key. The settings UI reads the latest version, caches for 60 seconds, and saves new versions. Removing a key adds an empty version; administrators retain older versions for recovery.

Run `scripts/configure-google-identity.sh` after DOKS rotates its service-account signing keys (and following cluster upgrades). It updates the provider with `kubectl get --raw /openid/v1/jwks`. Google cannot discover this private issuer automatically. Verify both provider configuration status endpoints from a Trends pod after updates. [Google's external Kubernetes federation guide](https://docs.cloud.google.com/iam/docs/workload-identity-federation-with-kubernetes)

The deployment runner uses the owner-supplied `codex-trends@macro-events-882dcb.iam.gserviceaccount.com` credential at `/home/codex/.config/gcloud/codex-trends.json` (0600). This administrative key stays outside Git and Kubernetes. doctl's `freddie-pki` credential remains in its protected config and is passed only into Terraform's process environment. Terraform state necessarily contains generated database credentials and remains owner-only. Preserve state and restrict access to its backups.

DigitalOcean manages cluster image-pull credentials through the DOCR integration. App pods reference its `freddierice-systems` pull Secret. Builds use an expiring one-hour push credential, removed from the runner after the build. Images contain no `.env`, SQLite databases, API keys, Terraform state, or Google credentials.

Health's refresh/access tokens, provider ownership, and OAuth state are application data inside its PostgreSQL database; the import preserves them. They are distinct from the OAuth client credentials stored in Secret Manager. The Freddie CA signing keys remain on the existing CA; this cluster contains only its public root. cert-manager generates and maintains its ACME account key in `systems/freddie-acme-account` and the leaf certificate/key pairs in `systems/health-tls` and `systems/trends-tls`. Gateway listeners reference those Kubernetes TLS Secrets directly. These rotating controller-managed keys stay in Kubernetes; Google holds the app/deployment credentials listed above. Back up the account and TLS Secrets as part of cluster recovery. The old gateway volume is unmounted and retained only for recovery.
