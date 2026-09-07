# Push-to-deploy

A push to `main` in Health or Trends runs its checks, builds the tested commit,
pushes an immutable DOCR image, and automatically requests a deployment here.
Every job runs on GitHub-hosted Ubuntu. There is no production approval click.

The app workflow reports that its deployment request was accepted. The
`Deploy application` workflow in this repository reports the production result.
It queues requests for the shared Helm release, rejects superseded source
commits, and updates only the selected app's image. Successful image digests and
source revisions are committed to `kubernetes/values.production.yaml`.

## Credentials and network access

GitHub OIDC authenticates to Google Workload Identity Federation. Three service
accounts have access only to their assigned Secret Manager secrets. Trust is
restricted to immutable owner/repository IDs, `main`, the exact release/deployment
workflow, its expected event, and GitHub-hosted runners. PR checks receive no
cloud credentials. Only the provider resource name and service-account email are
stored in GitHub repository variables.

| Google secret | Consumers | Purpose |
| --- | --- | --- |
| `systems-actions-digitalocean` | Health and Trends release jobs | Registry-only publishing credential; each build generates a 30-minute registry login |
| `systems-actions-digitalocean-deploy` | Central deployment job | Read registry metadata, temporarily update the DOKS API firewall, and obtain expiring Kubernetes credentials |
| `systems-actions-github-dispatch` | All three release jobs | Dispatch to this repository and read both private app repositories for source/schema verification |

OIDC replaces stored Google service-account keys. DigitalOcean and GitHub API
credentials remain in Secret Manager. The credential bootstrap generates a
durable registry-only token and verifies it is denied Kubernetes access. The
deployment token is copied from the existing `systems-digitalocean-token` secret
and retains that token's account permissions; this is not a Deployment-only RBAC
identity. A supplied GitHub token must be able to dispatch here (Contents: write)
and read Health and Trends. Prefer a credential restricted to these repositories.
Using `--use-gh-session` instead retains the authenticated CLI session's full
permissions. Revoke or rotate these credentials through their providers and
publish replacement Secret Manager versions when needed.

Deployment needs only the public DOKS API on HTTPS. The firewall helper temporarily
adds the runner's individual public IPv4 `/32`, retaining the existing droplet/NAT
allowances. The runner obtains a kubeconfig valid for 55 minutes and uses the
existing Helm release's permissions, including release Secrets and shared chart
resources. It has no direct route to private workers or PostgreSQL.

Readiness/page checks use a temporary localhost Kubernetes port-forward to the
gateway, preserving each app's TLS SNI, Host header, and private CA verification.
The job's `always()` cleanup removes its firewall entry and kubeconfig. If firewall
cleanup fails, the job fails and saves its non-secret cleanup state as an artifact.
A lost runner or forced cancellation can prevent cleanup: the runner `/32` is
logged before the firewall update, so remove that exact entry using the procedure
below. DigitalOcean's firewall entries have no automatic expiry. Do not run
Terraform or edit the API firewall concurrently with a release; DigitalOcean
does not expose conditional updates for that address list.

## One-time activation

From this repository on the existing administration host, with authenticated
`gcloud`, `doctl` (`freddie-pki` context), and `gh`:

```sh
python3 scripts/configure-actions-identity.py
python3 scripts/configure-actions-identity.py --apply
# Prefer supplying a dedicated credential through SYSTEMS_DEPLOY_TOKEN.
python3 scripts/configure-actions-secrets.py --apply
```

If deliberately using the current GitHub CLI credential for dispatch/source
reads, the final command is:

```sh
python3 scripts/configure-actions-secrets.py --apply --use-gh-session
```

The identity script creates the dedicated `systems-actions` pool/provider, three
service accounts, empty secrets when absent, scoped IAM bindings, and the two
repository variables in each repository. It reuses matching resources and refuses
to overwrite conflicting trust. The secret script sends values through subprocess
input; it prints neither credentials nor secret payloads.

Publish this repository's deployment workflow and verified production revision
baselines on `main` before publishing the two app workflows on their `main`
branches. Those app pushes are the first end-to-end release test. Confirm both
source workflows, central deployments, recorded digests, and firewall cleanup.
Ordinary future pushes need no additional setup.

## Release safeguards and recovery

- Tests and publishing run only from the app's committed source. Google credential
  files stay outside the Docker build context and deployment Git checkout.
- The receiver requires the current app `main` SHA and its matching DOCR tag/digest.
  A delayed request for an older commit is skipped. Duplicate requests are safe.
- Migration runner/SQL changes stop automatic deployment. Run and verify the
  migration separately using the [migration runbook](migration.md), then invoke
  `scripts/deploy-app.py` manually with the release arguments and
  `--schema-verified`. That operator-only flag bypasses the migration-file gate;
  the source, image, and chart checks still apply. The GitHub workflow never
  accepts this flag. Image rollback does not reverse a database migration.
- The rendered chart may differ from the Helm release only in the selected app's
  image. Platform/configuration changes need a separate reviewed Helm update. The
  app job never calls `kubernetes/deploy.sh` or upgrades platform controllers.
- Apps retain one replica and `Recreate` to avoid duplicate background workers.
  Brief release interruptions remain expected.
- Helm uses `--atomic --wait`; failed gateway smoke checks roll back to the prior
  Helm revision. A successful rollout followed by a Git push failure is reported
  as failed bookkeeping. Retry the same central run after fixing Git access; the
  helper recognizes an already-deployed image and records it without rebuilding.

For firewall cleanup failure, download the `firewall-cleanup-<run-id>` artifact,
then run from an authenticated administration environment:

```sh
# DIGITALOCEAN_ACCESS_TOKEN must be available to the helper.
python3 scripts/actions-cluster-access.py cleanup --state /path/to/systems-firewall.json
```

If a runner disappeared before uploading its state, use its logged `/32` to remove
only that entry from the cluster's control-plane firewall. Preserve the existing
administrator and NAT entries.

For code rollback, revert the bad application change and push `main`; the same
pipeline tests/builds/deploys the revert. For immediate operator rollback, apply
the previous compatible image using the runbook and reconcile its recorded image
and source revision before the next automatic release.

## Validation

```sh
make init check
python3 scripts/actions-cluster-access.py prepare --state /tmp/firewall-preview.json --dry-run
python3 scripts/deploy-app.py --app health --source-sha FULL_SHA \
  --image-digest sha256:DIGEST --run-id BUILD_RUN_ID --dry-run
```

The dry runs require the appropriate read credentials but do not change cluster
or recorded production state. Release tests cover stale requests, image/schema
checks, shared-resource protection, failure rollback, Git races, TLS routing,
firewall ownership/cleanup, and OIDC trust boundaries.

GitHub's documented `concurrency.queue: max` preserves up to 100 pending releases
instead of replacing the other app's pending request. Actionlint 1.7.12 predates
this field; when using that version, suppress only its unsupported-field diagnostic:

```sh
actionlint -ignore 'unexpected key "queue" for "concurrency" section' .github/workflows/deploy.yml
```

References: [GitHub concurrency queues](https://docs.github.com/en/actions/how-tos/write-workflows/choose-when-workflows-run/control-workflow-concurrency),
[Google deployment federation](https://docs.cloud.google.com/iam/docs/workload-identity-federation-with-deployment-pipelines),
[DOKS API firewall](https://docs.digitalocean.com/products/kubernetes/how-to/add-control-plane-firewall/).
