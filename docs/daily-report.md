# Daily printed report

The report source is `freddierice/time`. The original installation is
`daily@137.184.18.81:/home/daily/time`, scheduled at `30 5 * * *` in
`America/Chicago`. It syncs Whoop, Withings and Strong, uploads `health.db` to
Google Drive, renders the planner with Todoist and Google Calendar, and prints
Letter paper with `sides=two-sided-long-edge`.

## Cluster configuration

The systems chart manages the `daily-report` CronJob. Production uses an
immutable image in `registry.digitalocean.com/freddierice-systems/daily-report`.
The schedule and timezone preserve the original 5:30 a.m. Central run, including
daylight saving time. `concurrencyPolicy: Forbid`, `backoffLimit: 0`, and the
application's persistent print records protect against duplicate submissions.
The runner checks the printer's terminal job state; local queue acceptance alone
is not reported as a successful print.

`daily-report-data` is a retained block-storage PVC mounted at `/data`. It holds
the SQLite database, refreshed OAuth token files, generated PDF and print records.
The pod runs as UID/GID 1000 with a read-only root filesystem, a writable `/tmp`,
and no Kubernetes API token. It does not pull Git or install packages at runtime.

The `daily-report-runtime` Secret holds provider client settings and the Todoist
token. `daily-report-google` supplies `google.json` at
`/etc/daily-report/google.json` for the existing Google Drive integration.
The corresponding Google Secret Manager names are
`systems-daily-report-runtime` and `systems-daily-report-google`; both are included
in `kubernetes/google-secrets.yaml`. Mutable provider OAuth tokens belong on the
PVC, so replacing a Secret cannot revert a rotated refresh token.

## Printer route and migration status

The source host's printer connection was already broken at discovery on
2026-09-07. Its `bluesummer.biz` Tailscale identity and its Home Assistant subnet
router expired on August 27. The last completed print was job 184 on August 27;
jobs 185–195 (August 28–September 7) were canceled after three hours with
`printer is unreachable`. No print jobs remained queued.

The cached router is `homeassistant.taile78e54.ts.net` (`100.64.102.10`), advertising
only `10.0.0.33/32`. Systems belongs to the `freddie.rice@gmail.com` tailnet
(`impala-hen.ts.net`), a different tailnet.
For the chart's printer egress Service to work, an always-on host on the printer
LAN must advertise and have approval for `10.0.0.33/32` in the systems tailnet.
Its policy must allow the systems printer proxy to TCP 631. Cross-tailnet device
sharing does not share subnet routes. See the [Tailscale subnet egress
guide](https://tailscale.com/docs/kubernetes-operator/egress/access-ip-behind-subnet-router).

When that route is available, set `dailyReport.printer.enabled: true`. The
operator maps `daily-report-printer` to the printer's IP, and the report uses
`ipp://daily-report-printer.systems.svc.cluster.local/ipp/print`. The target supports
PDF directly through IPP Everywhere. `dailyReport.printer.uri` can instead point
to an explicitly configured private print endpoint.

The cluster schedule is staged **suspended** pending printer connectivity and
verification. The source cron is still enabled. Do not enable both schedules.
Existing source data also has a Whoop refresh failure and stale health data;
migration preserves the provider configuration and does not repair those
pre-existing provider issues.

The protected initial recovery snapshot is
`gs://freddie-systems-migration-186933910776/daily-report-2026-09-07/`.
It contains source revision `5ad1449013cd3f8053487d61d492fab828182a49`, the original
runtime files and a database integrity/count record: 122 health rows and one
workout. Public access prevention and uniform bucket-level access are enforced.

## Staging verification — 2026-09-07

Helm revision 11 installed the suspended CronJob, ServiceAccount and 1 GiB PVC.
Comparison with the previous rendered release showed no changed or removed
existing resources. The printer Service remains disabled while its route is
unavailable. Health, Trends, Traefik and the systems DNS replicas stayed Ready.

The image was built from clean local Time commit
`8baafa098d90034a7efcd1088cb9e03b9b132e0e`, with digest
`sha256:73935ba3212ab594c069d6095f56a283079acee4a626f80196a6a8f9fbe610cc`.
Twelve application tests and 78 deployment tests passed, as did Helm lint and
server-side validation. The live container verified UID 1000, read-only root
filesystem and the absence of runtime data/credentials in `/app`. All five
seeded database/token files matched their source SHA-256 checksums. SQLite
integrity and the 122/1 row counts remained valid after rendering.

`preflight --skip-printer` and `generate-only` succeeded in a temporary pod using
the CronJob's image, environment, volumes and security settings. The generated
10,527-byte PDF has two landscape Letter pages (792 × 612 points); its decoded
page content is identical to the source's September 7 report. Calendar/Todoist
fetches completed without new warnings. A read-only Drive metadata request
authenticated successfully and confirmed edit capability on the existing
`health.db` backup. No health sync, backup upload, or physical print was triggered
by these checks. The temporary pod and builder were removed after verification.

The protected recovery prefix also holds `staged-data.tar`,
`render-verification.json`, `image-metadata.json` and `time-cluster.bundle`.
The bundle preserves the complete tested Time commit. Publishing that commit
to `freddierice/time` is pending: GitHub rejected the source host's read-only
deploy key. A dedicated `codex-time-deploy` public key has been supplied for
repository write access. Systems configuration is already published on its
repository's `main` branch.

## Cutover and recovery

1. Restore the printer route and verify the container's `preflight` command from
   a temporary Job using the production CronJob pod template. This queries
   printer capabilities without submitting a print.
2. Back up `crontab -u daily -l` on the source. Remove only the exact daily report
   entry, and verify no report process remains active. Keep its source, data and
   credentials for recovery.
3. With the CronJob still suspended and no Job using its PVC, transfer a final
   consistent `health.db` and the current OAuth JSON files from the source to
   `/data`. Verify database integrity, row counts and file checksums. Do not copy
   `.envrc` or the Google private key into the image or PVC.
4. Run a single production Job and verify report generation, Drive upload and
   the printer's completed job state. The original job must remain disabled.
5. Commit `dailyReport.suspend: false` in production values and upgrade only the
   systems Helm release. Check the CronJob's next 5:30 a.m. Central execution.

Use `kubectl --context do-nyc1-systems -n systems get cronjob,jobs` and `kubectl
--context do-nyc1-systems -n systems logs job/JOB_NAME` to inspect executions.
Do not blindly delete print records or retry an ambiguous submission; inspect
the printer's job history first to avoid printing the same report twice.

For rollback, suspend the cluster schedule first and wait for any active report
Job to stop. Preserve the current PVC and refreshed tokens. Reconcile the most
recent token files, database and print status before restoring the old cron.
The source host also needs a working printer route; simply restoring its cron
does not fix its expired Tailscale login. Retained PVCs and block volumes require
explicit administrator cleanup when recovery is no longer needed.
