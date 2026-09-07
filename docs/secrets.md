# Secret storage direction

Google Secret Manager is a suitable central store for the Tailscale operator OAuth
credential and long-lived application API credentials. The operator credential is
staged by the owner in project `186933910776` under
`systems-cluster-tailscale-id` and `systems-cluster-tailscale-secret`.
`kubernetes/bootstrap-google-secrets.py` reads the latest versions through an
authenticated gcloud CLI and streams them into `tailscale/operator-oauth`.
It does not print or persist secret payloads locally.

For the initial deployment, gcloud authenticates as
`codex-trends@macro-events-882dcb.iam.gserviceaccount.com`, using the Owner service
account credential supplied by the project owner at
`/home/codex/.config/gcloud/codex-trends.json` (mode `0600`). This administrative
credential remains on the deployment droplet, outside the repository; it is not
installed in Kubernetes. The operator receives only its two Tailscale values.

A future continuous synchronization option is External Secrets Operator in DOKS, authenticated with
Google Workload Identity Federation using narrowly scoped Kubernetes identities.
Grant access to individual required secrets without installing the administrative
Google service account JSON key in the cluster. External Kubernetes federation needs
explicit issuer/JWKS and audience configuration; GKE-specific annotations alone do
not configure it for DOKS. Keep federation public-key rotation in the runbook.

References: [Google's external Kubernetes federation guide](https://docs.cloud.google.com/iam/docs/workload-identity-federation-with-kubernetes)
and [External Secrets' Google provider](https://github.com/external-secrets/external-secrets/blob/main/docs/provider/google-secrets-manager.md).

Until continuous synchronization is configured, operator credentials are pulled
on demand by the Google bootstrap, or can be supplied locally
to `scripts/bootstrap-operator.sh`, which streams them into a Kubernetes Secret
without command-line secret arguments or temporary files. The DigitalOcean token
already in doctl's `freddie-pki` context is read only into Terraform's process
environment by `scripts/with-doctl.py`.

The database bootstrap copies the generated database credentials from protected
Terraform state into app-specific Kubernetes Secrets. These secrets do not become
safer merely by also copying them to an external store: doctl config, Terraform
state/backups, and Kubernetes RBAC all need appropriate access restrictions. Remove
superseded bootstrap copies when ownership has moved to the selected secret store.

Kubernetes Secret synchronization does not restart apps whose secrets arrive via
environment variables. A credential rotation must coordinate database/provider
changes, Secret synchronization, pod restarts, and verification. The existing
Freddie CA signing keys remain outside this cluster; only its public root is used.
