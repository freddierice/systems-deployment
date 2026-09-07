# Daily printed report

The report source is `freddierice/time`. The original installation was
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

## Printer route and migration status — 2026-09-07

The source host's printer connection was already broken at discovery on
2026-09-07. Its `bluesummer.biz` Tailscale identity and its Home Assistant subnet
router expired on August 27. The last completed print was job 184 on August 27;
jobs 185–195 (August 28–September 7) were canceled after three hours with
`printer is unreachable`. No print jobs remained queued.

Home Assistant is now connected to the systems tailnet
(`freddie.rice@gmail.com`, `impala-hen.ts.net`) at `100.84.140.78`, with its
advertised printer route `10.0.0.33/32` approved. This supplies the subnet route
in the same tailnet as the systems operator.

Helm revision 13 enabled `dailyReport.printer.enabled` and installed the
`daily-report-printer` ProxyClass with `tailscale.acceptRoutes: true`, recorded
in systems commit `00e9224`. The printer Service uses that ProxyClass to reach
`10.0.0.33:631`; the report's URI is
`ipp://daily-report-printer.systems.svc.cluster.local/ipp/print`. The target
supports PDF directly through IPP Everywhere. See the [Tailscale subnet egress
guide](https://tailscale.com/docs/kubernetes-operator/egress/access-ip-behind-subnet-router).
`dailyReport.printer.uri` can instead point to an explicitly configured private
print endpoint.

The source daily report cron entry is disabled. Final state transfer, printer
preflight and the first cluster report completed successfully; the printer
reported job 341 completed for the September 7 report. Helm revision 14 is
deployed with `dailyReport.suspend: false`; the recurring cluster schedule is
active. Its next scheduled run is September 8 at 5:30 a.m. America/Chicago
(10:30 UTC). Do not enable both schedules.

Whoop's invalid refresh-token error appears in source logs from August 8 onward. Withings refreshed
successfully on the source on September 7 in the morning, but an invalid
refresh-token error was first observed in the cluster during cutover. The
Withings tokens match across the source, staged and final snapshots, and its
client credentials and refresh request are unchanged. No newer credential was
recoverable. Both providers need OAuth reauthorization to restore fresh health
data. Until then, health values can remain cached, as permitted by the existing
report behavior. Report generation, Drive upload and printing succeeded despite
these provider warnings.

The protected initial recovery snapshot is
`gs://freddie-systems-migration-186933910776/daily-report-2026-09-07/`.
It contains source revision `5ad1449013cd3f8053487d61d492fab828182a49`, the original
runtime files and a database integrity/count record: 122 health rows and one
workout. Public access prevention and uniform bucket-level access are enforced.

## Staging verification — 2026-09-07

Helm revision 11 installed the suspended CronJob, ServiceAccount and 1 GiB PVC.
Comparison with the previous rendered release showed no changed or removed
existing resources. At that initial staging step the printer Service was
disabled while its route was unavailable. Health, Trends, Traefik and the systems
DNS replicas stayed Ready. The printer route was subsequently configured in
revision 13 as described above.

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
The bundle preserves the complete tested initial Time commit. The container
migration and subsequent Actions setup are now published on `freddierice/time`'s
`main` branch. Systems configuration is also published on its repository's
`main` branch.

## GitHub Actions release setup — 2026-09-07

Time commit `c91a99c67d0168018470e43fa139e7c8590d2522` adds its release workflow
and isolated image check. Pull requests run unit tests, compilation and an
offline container check. A main push publishes a DOCR image and calls the
systems reusable deployment workflow with `app: daily-report`. Only its image
and source revision are updated; releases do not run a report or change the
CronJob's schedule/suspension. See [push-to-deploy](github-actions.md).

The live Google provider now accepts the exact Time main-push release workflow,
and `systems-ci-daily-report` has access only to the existing registry publishing
credential. Verification found no project-role, runtime-secret or deployment-
secret grants for that publisher. Existing Health/Trends trust was preserved.
The registry publisher credential remains denied Kubernetes account access.

All 104 application/deployment tests and actionlint checks passed. Before
publication, a candidate image passed the reusable deployer's real cluster smoke
check with temporary storage, no runtime credentials, and denied network access.
The CronJob spec was identical before and after the check; the smoke pod and its
policy were removed.

The first [GitHub Actions release, run 34166494538](https://github.com/freddierice/time/actions/runs/34166494538),
successfully published Time commit
`c91a99c67d0168018470e43fa139e7c8590d2522` and deployed the resulting image:

```text
registry.digitalocean.com/freddierice-systems/daily-report@sha256:93e6316c6b2986722771de056e00b9b8ce306efac51703663b8fb0cebab2ba96
```

Systems release commit `979b444ef9262ec54acc01a51fdea60eb18bc6cd`, recorded at
22:26:39 UTC, pins that image and source revision in production values. The
release updated the live CronJob image and preserved its then-current
`suspend: true`. The isolated smoke pod, its network policy, and the shared
release lock were cleaned up. This confirms the automatic image release path.
The same CI image then completed the physical cutover below.

## Cutover and recovery

The production `preflight` passed through the printer Service using actual IPP
responses. The printer advertised acceptance of PDF, Letter, color, 600 dpi and
long-edge duplex.

Only the exact daily report cron line was removed from the source. Its original
crontab was backed up at
`/home/daily/.local/state/daily-report-cutover-20260907/crontab.before` and archived
as `source-cron-before-cutover.txt` in the protected recovery prefix above. The
GCS copy remains the recovery copy after source-host cleanup.

A final consistent SQLite snapshot and all four OAuth token files were copied
to the PVC while the cluster schedule was suspended. All five file hashes
matched, SQLite integrity passed, and the database contained 122 health rows and
one workout. `runtime-final.tar` and its checksum record are retained in the
same protected recovery prefix. Runtime environment settings and the Google
private key remain in Secrets, outside the image and PVC.

Manual Job `daily-report-cutover-20260907`, using the CI image above, succeeded
at 22:33:06 UTC on September 7. Google Drive upload and PDF generation succeeded,
and the printer reported terminal state 9 (`completed`) with reason
`job-completed-successfully` for print job 341. The print record on the PVC shows
completion at 22:33:03 UTC. A subsequent verification run returned
`Report 2026-09-07 already completed; skipping`, confirming that another run does
not submit the same report again. SQLite integrity remained valid.

The protected recovery prefix also contains `cutover-data.tar`,
`cutover-verification.txt` and `cutover-job.log`. This final archive preserves the
rotated calendar tokens and completed print record along with the database.

The source cron must remain disabled. Helm revision 14 is deployed, and the live
CronJob confirms `suspend: false`, schedule `30 5 * * *`, and timezone
`America/Chicago`. Activation was verified at 22:34:37 UTC and recorded in
published systems commit `80946a4`. Check the first scheduled cluster execution
on September 8 at 5:30 a.m. America/Chicago (10:30 UTC).

Use `kubectl --context do-nyc1-systems -n systems get cronjob,jobs` and `kubectl
--context do-nyc1-systems -n systems logs job/JOB_NAME` to inspect executions.
Do not blindly delete print records or retry an ambiguous submission; inspect
the printer's job history first to avoid printing the same report twice.

For rollback, suspend the cluster schedule first and wait for any active report
Job to stop. Preserve the current PVC and refreshed tokens. Recovery on the
source host after cleanup requires fresh SSH access and rebuilding the original
installation from the protected GCS archives. Reconcile the most recent token
files, database and print status before restoring its runtime configuration or
cron; an older archive must not replace current rotated tokens or completed
print records. The source host also needs a working printer route; rebuilding
its installation does not fix its expired Tailscale login. Retained PVCs and
block volumes require explicit administrator cleanup when recovery is no longer
needed.

## Source cleanup — completed 2026-09-07

The original `/home/daily/time` installation and both migration directories,
`/home/daily/.local/state/time-cluster-migration` and
`/home/daily/.local/state/daily-report-cutover-20260907`, were removed from
`ssh-tunnel`. Their absence and the absence of the daily report cron entry were
verified before closing SSH access.

Exactly one migration key entry was revoked from `/root/.ssh/authorized_keys`,
preserving the other two entries. A fresh connection using only the migration
key, with no SSH agent or multiplexed connection, failed with
`Permission denied (publickey)`. The local private and public key files were
also deleted. The source host no longer provides an installed fallback or
migration SSH access; recovery requires the protected GCS archives and fresh
access as described above. `source-decommission.txt` records the cleanup in the
protected recovery prefix.

Metadata-only checks on September 7 confirmed that `source.tar` (532,480 bytes),
`runtime-final.tar` (71,680 bytes), and `cutover-data.tar` (81,920 bytes) still
exist in the protected recovery prefix. Their contents were not downloaded for
this check. Cluster workloads, the active schedule, retained PVC, and protected
GCS recovery objects are outside this source-host cleanup.
