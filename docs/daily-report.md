# Daily printed report

The report source is `freddierice/time`. The original installation was
`daily@137.184.18.81:/home/daily/time`, scheduled at `30 5 * * *` in
`America/Chicago`. The database-free report reads measurements and exercise
records directly from Health each time it generates a report, renders the
planner with Todoist and Google Calendar, and prints Letter paper with
`sides=two-sided-long-edge`. It does not create, seed, migrate, or upload a local
health database. The dated sections below retain the earlier deployment history.

## Cluster configuration

The systems chart manages the `daily-report` CronJob. Production uses an
immutable image in `registry.digitalocean.com/freddierice-systems/daily-report`.
The schedule and timezone preserve the original 5:30 a.m. Central run, including
daylight saving time. `concurrencyPolicy: Forbid`, `backoffLimit: 0`, and the
application's persistent print records protect against duplicate submissions.
The runner checks the printer's terminal job state; local queue acceptance alone
is not reported as a successful print.

`daily-report-data` is a retained block-storage PVC mounted at `/data`. The
database-free report uses it for the generated PDF, execution lock and print
records, plus writable Calendar OAuth tokens (`gcal_tokens_*.json`). Existing
SQLite databases, migration backups and old health-provider OAuth token files
remain stored there as legacy artifacts; the report does not read or update them.
The pod runs as UID/GID 1000 with a read-only root filesystem, a writable `/tmp`,
and no Kubernetes API token. It does not pull Git or install packages at runtime.

The `daily-report-runtime` Secret holds the Todoist token and legacy provider
settings. Time no longer uses its Whoop or Withings credentials; Health owns
provider synchronization. The chart retains `daily-report-google`, which supplies
the legacy `google.json` at `/etc/daily-report/google.json`; the database-free
report does not use it or upload backups to Google Drive.
The corresponding Google Secret Manager names are
`systems-daily-report-runtime` and `systems-daily-report-google`; both are included
in `kubernetes/google-secrets.yaml`. Removing the database from the application
does not delete the retained Secrets or stored OAuth tokens. Calendar tokens
continue to refresh on the PVC; replacing a Secret cannot revert those refreshes.

## Health data source

The CronJob uses `HEALTH_API_URL=http://health.systems.svc.cluster.local:8000` and
`HEALTH_API_HOST=health.freddie.xyz` to read Health's measurement history and
selected activity source. The client sends the canonical Host and
`X-Forwarded-Proto: https` headers over the
trusted internal service route, as the existing Health probes do. The
`daily-report-to-health` NetworkPolicy permits only report pods in the same
namespace to reach Health on TCP 8000. No provider OAuth calls or new credentials
are needed in Time.

`dailyReport.health.url` can override the base URL; an empty value selects the
Health Service in the release namespace. `dailyReport.health.host` controls the
optional Host override and forwarded HTTPS header. Set it empty when using a
normal HTTPS URL that does not need proxy headers. Public HTTPS access also
requires a reachable route and trust for the private CA.

Health provides weight history and the activity source selected in its catalog:
Fitbit, manually recorded activity, or locally completed workouts. Time uses the
selected source without mixing activity from the others. Health has no HRV,
resting heart rate or sleep measurements to export, so the report omits those fields. Time no longer syncs Strong directly.
Provider reauthorization, freshness and source corrections are managed in Health.
Legacy provider rows remain archived in the retained database. New reports read
Health directly, keep the response in memory, and never fall back to that database
or a local Health cache.

The chart's Health environment and NetworkPolicy must already be applied before
releasing the Time image: the reusable app deployer permits image changes only.
Publish the deployment helper's database-free release support before pushing the
Time revision that removes `db.py`. A candidate daily-report revision without
`db.py` needs no database migration, seed, backup upload or `--schema-verified`,
including the transition from the previous database-backed image. The source,
digest, chart, scheduling and isolated-image checks still apply. A revision that
restores `db.py`, or changes it while retaining the database, still requires
operator schema verification. Health and Trends retain their migration guards.
Preserve the schedule, PVC, legacy artifacts and print records. Verify a live
Health read and report generation without submitting a new print job before the
next scheduled run.

When GitHub Actions cannot be read through an authenticated GitHub API, the
published DOCR image's provenance contains its exact Actions run URL. The local
operator helper `/tmp/time-health-image-provenance.py SOURCE_SHA` reads that
provenance and prints only the image digest and run URL. It obtains a temporary
read-only registry credential through the existing `freddie-pki` doctl context;
credentials are not saved or printed. The helper is a local inspection aid and
is not required by the scheduled report or normal release workflow.

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

At cutover, Whoop's invalid refresh-token error appeared in source logs from
August 8 onward. Withings refreshed successfully on the source on September 7
in the morning, but an invalid
refresh-token error was first observed in the cluster during cutover. The
Withings tokens matched across the source, staged and final snapshots, and its
client credentials and refresh request are unchanged. No newer credential was
recoverable. The old direct-provider report needed OAuth reauthorization to
restore fresh health data and allowed cached values. Report generation, Drive
upload and printing succeeded despite these provider warnings.

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

The container migration and subsequent Actions setup are published on
`freddierice/time`'s `main` branch. Systems configuration is also published on
its repository's `main` branch.

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

Only the exact daily report cron line was removed from the source.

A final consistent SQLite snapshot and all four OAuth token files were copied
to the PVC while the cluster schedule was suspended. All five file hashes
matched, SQLite integrity passed, and the database contained 122 health rows and
one workout. Runtime environment settings and the Google private key remain in
Secrets, outside the image and PVC.

Manual Job `daily-report-cutover-20260907`, using the CI image above, succeeded
at 22:33:06 UTC on September 7. Google Drive upload and PDF generation succeeded,
and the printer reported terminal state 9 (`completed`) with reason
`job-completed-successfully` for print job 341. The print record on the PVC shows
completion at 22:33:03 UTC. A subsequent verification run returned
`Report 2026-09-07 already completed; skipping`, confirming that another run does
not submit the same report again. SQLite integrity remained valid.

The source cron must remain disabled. Helm revision 14 is deployed, and the live
CronJob confirms `suspend: false`, schedule `30 5 * * *`, and timezone
`America/Chicago`. Activation was verified at 22:34:37 UTC and recorded in
published systems commit `80946a4`. Check the first scheduled cluster execution
on September 8 at 5:30 a.m. America/Chicago (10:30 UTC).

Use `kubectl --context do-nyc1-systems -n systems get cronjob,jobs` and `kubectl
--context do-nyc1-systems -n systems logs job/JOB_NAME` to inspect executions.
Do not blindly delete print records or retry an ambiguous submission; inspect
the printer's job history first to avoid printing the same report twice.

For an image rollback, suspend the cluster schedule first and wait for any
active report Job to stop. Preserve the live PVC, legacy artifacts and completed
print records, and reconcile print status before any manual run. Rolling back to
an image that uses SQLite requires verifying its compatibility with the retained
database; restoring `db.py` is subject to the schema gate. The source host no longer has
an installed fallback or migration SSH access. Rebuilding there would require
fresh access, the published Time repository, current runtime state and a working
printer route.

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
migration SSH access.

## Migration archive cleanup — 2026-09-07

At the user's request after migration completed, the migration archives and
verification records under
`gs://freddie-systems-migration-186933910776/daily-report-2026-09-07/` and the local
`/home/codex/.local/state/daily-report-migration` directory were deleted. The
source, runtime and cutover archives, original crontab copy, Git bundles, and
decommission record are no longer available for recovery. Verification found no
live, noncurrent or soft-deleted archive objects under that prefix.

The report continued to use its live cluster PVC, Secrets, print records and
active schedule, with source code in the published repositories. Google Drive
backups of `health.db` were outside this migration-archive deletion. The later
database-free report no longer uploads them; existing backups remain retained.


## Health-only report release — 2026-09-07

Time commit `9fe886cb042904e72837442bf3d0798d56dcedaa` replaces all direct health
provider inputs with Health. The PDF shows weight and exercise record counts;
its separate Health cache never reads archived Whoop, Withings, or Strong rows.
Calendar and Todoist remain report inputs, and Google Drive remains a backup
output. The 37 Time tests and 94 deployment tests passed. The
[Actions build](https://github.com/freddierice/time/actions/runs/34169442283)
published the tested image after its isolated container checks:

```text
registry.digitalocean.com/freddierice-systems/daily-report@sha256:5b7a2f166e3ad70a47c792b5b701cf17b18229544b78a24521ebd5f338192a08
```

Systems commit `41f135a` installed the Health route and environment at Helm
revision 15. The additive SQLite migration was then executed from the exact
committed source under the report data lock. A consistent pre-migration backup
is retained on the report PVC at `/data/health.before-health-source-9fe886c.db`;
verification metadata is at `/data/health-source-migration-9fe886c.json`. Legacy
row/schema hashes, OAuth files, production PDF, and print records were
preserved, and SQLite integrity passed. A successful Health sync populated only
the new cache tables. The reviewed migration allowed the existing deployment
helper to run with `--schema-verified`; all its source, digest, chart, scheduling,
and isolated-image checks remained active.

Systems release commit `c09c5b9` records the deployed image at Helm revision 16.
The shared production lock was released after deployment. The CronJob remains
active at `30 5 * * *` in `America/Chicago`. Production preflight passed, and the
new image generated a 10,198-byte PDF with two landscape Letter pages in a
temporary path. Calendar and Todoist fetched without warnings. The production
PDF and print markers remained unchanged; verification did not print or upload
anything, and all temporary pods were removed.

Health currently has no weight or exercise records within the latest 30-day
window: its newest weight is dated July 20 and selected exercise record July 8.
The empty report values reflect that Health history; legacy provider data is
not substituted. Provider freshness is managed in Health.


## Database-free report release — 2026-09-08

Time source `ff9416c53129d754394c1a2b2e4a531605523602` removes the SQLite layer,
health sync/cache, Drive database upload, and unused direct-provider clients.
Each PDF reads Health measurements and exercise records directly into memory.
A failed Health request stops generation and printing, including when an older
PDF exists. Calendar OAuth and durable print records remain on the PVC.

[Release run 34172372696](https://github.com/freddierice/time/actions/runs/34172372696)
passed all 42 Time tests, image checks, publishing and automatic deployment.
The schema helper accepted the removal without an operator migration override.
Systems commit `66d6b0b` records the deployed image:

```text
registry.digitalocean.com/freddierice-systems/daily-report@sha256:b5723d54de3659b5a74e07bb5298cdeffcff55a47bb7fccdae14b352302a8a2b
```

A temporary pod using that exact image passed production preflight and generated
a 10,198-byte, two-page landscape Letter PDF from live Health, Calendar and
Todoist. Python audit hooks rejected SQLite connections and database-file access
during generation; none occurred. Calendar and Todoist reported no warnings.
Health still reported no measurements in the requested 30-day window.
The production PDF, print markers and legacy database artifacts were unchanged;
no printing or upload occurred, and the temporary pod was deleted. The CronJob
remains enabled at `30 5 * * *` in `America/Chicago`.
