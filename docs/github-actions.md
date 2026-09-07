# Push-to-deploy

A push to `main` in Health or Trends runs its checks, builds the tested commit,
pushes an immutable DOCR image, and calls this repository's reusable deployment
workflow. Every job runs on GitHub-hosted Ubuntu. The app's Actions run reports
both the build and production deployment result. There is no approval click.

The shared workflow serializes updates to the existing Helm release, rejects
superseded source commits, and updates only the selected app's image. Successful
image digests and source revisions are committed to
`kubernetes/values.production.yaml` in this repository.

## Identity and credentials

GitHub OIDC authenticates to Google Workload Identity Federation. No personal
GitHub login, personal access token, repository variables, GitHub Actions secrets,
or Google service-account keys are required.

Trust is restricted to the immutable GitHub owner ID, the two app repository
names, `main` pushes, the exact caller workflow, and GitHub-hosted runners.
The reusable deployment job is identified by its exact `job_workflow_ref` claim
and receives a separate Google identity. App build identities can read only the
registry publisher secret. PR checks receive no cloud credentials. Recreating a
repository under the same trusted owner/name retains its trust.

| Google secret | Consumers | Purpose |
| --- | --- | --- |
| `systems-actions-digitalocean` | Health and Trends release jobs | Registry-only publishing credential; each build generates a 30-minute registry login |
| `systems-actions-digitalocean-deploy` | Shared deployment job | Read registry metadata, temporarily update the DOKS API firewall, and obtain expiring Kubernetes credentials |
| `systems-actions-git-key` | Shared deployment job | Existing SSH write deploy key, restricted to this repository, to record successful releases |

The caller's built-in `GITHUB_TOKEN`, with `contents: read`, reads that caller's
private source for revision and migration verification. Calling a public reusable
workflow does not require a cross-repository dispatch credential.

OIDC provides short-lived Google authentication; DigitalOcean and Git repository
credentials are retrieved from Secret Manager. The credential bootstrap verifies
that the registry publisher token is denied Kubernetes access. The deployment
token is copied from the existing `systems-digitalocean-token` recovery secret
and retains its account permissions. It is not a Deployment-only RBAC identity.
Rotate these provider credentials by publishing replacement secret versions.

## Cluster access and serialization

Deployment needs the public DOKS API on HTTPS. Private workers and PostgreSQL need
no direct inbound route from GitHub. The firewall helper temporarily adds the
runner's individual public IPv4 `/32`, preserving the administrator/NAT allowances.
The runner obtains a kubeconfig valid for 55 minutes. Helm needs access to release
Secrets and the resources managed by the existing shared chart. Gateway smoke
checks also need Pod discovery and `pods/portforward` access.

Readiness/page checks use a temporary localhost Kubernetes port-forward to the
gateway, preserving each app's TLS SNI, Host header, and private CA verification.
The job's `always()` cleanup removes its firewall entry and kubeconfig. If firewall
cleanup fails, the job fails and saves its non-secret cleanup state as an artifact.
A lost runner or forced cancellation can prevent cleanup: the runner `/32` is
logged before the firewall update, so remove that exact entry using the procedure
below. DigitalOcean firewall entries have no automatic expiry. Do not run Terraform
or edit the API firewall concurrently with a release; DigitalOcean does not expose
conditional updates for that address list.

GitHub concurrency groups are repository-scoped, so the two apps share an atomic
lock in the private bucket `gs://freddie-systems-actions-186933910776`. Only the
Google deployment service account has object access. A job waits up to 20 minutes
for the lock, then fails if production is still busy. The lock expires after two
hours if a runner disappears; normal cleanup releases it immediately. This lease
exceeds the entire job's 90-minute timeout. Retry a failed run after a stuck lease
expires, or remove the lock only after confirming its owner job has stopped.

## One-time activation

From the existing administration host, with authenticated `gcloud`, `doctl`
(`freddie-pki` context), and the existing deployment repository SSH key:

```sh
python3 scripts/configure-actions-identity.py
python3 scripts/configure-actions-identity.py --apply
python3 scripts/configure-actions-secrets.py --apply
```

The identity script creates the dedicated `systems-actions` pool/provider, three
service accounts, empty secrets when absent, scoped IAM bindings, and the private
lock bucket. It reuses matching resources and refuses conflicting trust. The
secret script sends credential values through subprocess input and prints no
credentials. `--git-key PATH` selects an existing deployment repository write key
if it is not at the administration host's default path.

Publish this repository's reusable workflow and production revision baselines on
`main` before publishing the two app workflows on their `main` branches. Those
app pushes are the first end-to-end release test. Confirm both app Actions runs,
recorded production digests, healthy applications, and firewall/lock cleanup.
Ordinary future pushes need no additional setup.

## Release safeguards and recovery

- Tests and publishing run only from the app's committed source. Google credential
  files stay outside the Docker build context and deployment Git checkout.
- Deployment requires the current app `main` SHA and its matching DOCR tag/digest.
  An older commit is skipped. Repeating a deployment is safe.
- Migration runner/SQL changes stop automatic deployment. Run and verify the
  migration separately using the [migration runbook](migration.md), then invoke
  `scripts/deploy-app.py` manually with the release arguments and
  `--schema-verified`. That operator-only flag bypasses the migration-file gate;
  source, image, and chart checks still apply. The GitHub workflow never accepts
  this flag. Image rollback does not reverse a database migration.
- The rendered chart may differ from the Helm release only in the selected app's
  image. Platform/configuration changes need a separate reviewed Helm update. The
  app job never calls `kubernetes/deploy.sh` or upgrades platform controllers.
- Apps retain one replica and `Recreate` to avoid duplicate background workers.
  Brief release interruptions remain expected.
- Helm uses `--atomic --wait`; failed gateway smoke checks roll back to the prior
  Helm revision. A successful rollout followed by a Git push failure is reported
  as failed bookkeeping. Rerun the failed app job after fixing Git access; the
  helper recognizes an already-deployed image and records it without rebuilding.

For firewall cleanup failure, download the `firewall-cleanup-<run-id>` artifact
from the app's Actions run, then run from an authenticated administration host:

```sh
# DIGITALOCEAN_ACCESS_TOKEN must be available to the helper.
python3 scripts/actions-cluster-access.py cleanup --state /path/to/systems-firewall.json
```

If a runner disappeared before uploading its state, use its logged `/32` to remove
only that entry from the control-plane firewall. Preserve administrator and NAT
entries.

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
actionlint .github/workflows/deploy.yml
```

Dry runs require appropriate read credentials but change no cluster or recorded
production state. Release tests cover stale commits, image/schema checks,
shared-resource protection, rollback, Git races, TLS routing, firewall cleanup,
conditional lock ownership, and OIDC trust boundaries.

References: [GitHub OIDC with reusable workflows](https://docs.github.com/en/actions/how-tos/secure-your-work/security-harden-deployments/oidc-with-reusable-workflows),
[Google deployment federation](https://docs.cloud.google.com/iam/docs/workload-identity-federation-with-deployment-pipelines),
[DOKS API firewall](https://docs.digitalocean.com/products/kubernetes/how-to/add-control-plane-firewall/).
