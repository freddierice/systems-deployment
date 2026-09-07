# Application contract and migration checklist

This repository prepares infrastructure only. It does **not** modify Health or Trends, convert their databases, build compatible images, move integration processes, or change production DNS. Both apps currently use Python `sqlite3`, SQLite-specific SQL, and filesystem-based initialization. Setting `DATABASE_URL` alone cannot migrate them.

## Required app changes before enabling Deployments

| Area | Required behavior |
| --- | --- |
| Connection | Read `DATABASE_URL`; connect to PostgreSQL when configured; fail startup on a missing/invalid PostgreSQL configuration in production rather than creating SQLite |
| Driver and SQL | Add a PostgreSQL driver; adapt placeholders, dict rows, generated IDs, `lastrowid`, conflict handling, triggers, PRAGMAs, and SQLite-specific functions |
| Schema | Versioned PostgreSQL migrations, run explicitly as Jobs before starting app processes; preserve foreign keys, unique/partial indexes, revisions, numeric precision and timestamps |
| Transactions | Preserve optimistic concurrency and write atomicity; replace `BEGIN IMMEDIATE` locking with explicit PostgreSQL transaction/locking semantics where needed |
| TLS | Honor `sslmode=verify-full` and the mounted database root CA; never disable verification |
| HTTP | Python 3.12+ on PATH; `python -m uvicorn health.app:app` or `trends.app:app`; bind port 8000 on the pod interface; trust forwarded protocol only from loopback and the configured pod CIDR |
| Health | `/health` returns 200 for process liveness; `/ready` checks PostgreSQL connectivity and migration version and returns a non-200 response if not ready |
| Filesystem | Run as UID/GID 1000 with a read-only root; temporary files go in `/tmp`; no persistent application data stored there |
| Integrations | Load provider credentials from environment Secrets and persist durable tokens/settings in PostgreSQL; keep background jobs singleton until a coordination mechanism exists |
| Packaging | Reproducible container builds from the app repo with the lockfile; publish to a private registry; pin deployed images by digest |

The chart supplies `HEALTH_HTTPS_ORIGIN`/`TRENDS_HTTPS_ORIGIN`, Trends' allowed hostname, and the shared `DATABASE_URL` contract. It intentionally bypasses droplet launch scripts that bind to loopback or discover a local Tailscale address. Local health probes send the canonical Host and HTTPS proxy header over loopback and accept only an actual 200, not a redirect.

Health has work-in-progress Measurements/integration changes in the droplet checkout. Include the intended version of that work when planning the migration. Its database includes workouts, routine/settings data, measurement data, integration accounts/tokens, and related history. Do not print or commit token rows.

Trends has multiple schema modules (`db.py`, `macro_db.py`, `stories_db.py`, `story_ideas_db.py`) and SQLite schema history through user version 10 at preparation time. Include journal data, macro boards, people/research, stories, caches/settings and audit history; enumerate the live schema rather than relying on this list as exhaustive. IBKR Gateway, ThetaData, and other droplet-local services are separate operational dependencies. A Kubernetes pod's `127.0.0.1` does not point to the droplet. Keep them on the droplet with explicit private egress routing or containerize them in a subsequent scoped change. Interactive login requirements remain.

## Rehearsal

1. Create consistent SQLite snapshots with each app's existing backup script; verify integrity and foreign keys. Record schema versions, table counts and hashes. Store backups off the droplet with restricted access.
2. Apply PostgreSQL schema migrations to a disposable rehearsal database. Import without dropping constraints. Preserve IDs, JSON text, booleans, decimal values, revisions, timestamps and relationships; reseed identity sequences beyond the largest imported IDs.
3. Confirm the import is all-or-nothing or safely resumable and refuses to overwrite an already-populated target. Verify every table's counts and representative records without printing sensitive records. Check audit history, revisions, foreign keys and generated ID sequences.
4. Run both apps' existing tests against PostgreSQL, adding tests for concurrent edits, transaction rollback, generated IDs, database isolation, reconnects, backups/restores and readiness behavior. SQLite-only test success is insufficient.
5. Rehearse with external syncs disabled to avoid duplicate integration work. Exercise app reads and representative writes, export, history, background jobs and provider callbacks in a controlled environment. Validate an actual PostgreSQL backup restore.
6. Verify the private gateway, custom hostname certificate chain, CA egress and HTTP-01 callback routing. Test access from an authorized tailnet device, denial from an unauthorized tailnet identity, and absence of public worker/app reachability. Verify the current DOKS/Cilium preflight. Do not use public exposure as a fallback for an unsuccessful private-access test.

## Final transfer and cutover

1. Finish the rehearsal and prepare a concrete infrastructure/DNS change plan. Preserve current DNS values, droplet app revisions and service configuration for rollback.
2. Stop both old application services and all background writers for a bounded maintenance window. Take fresh, integrity-checked snapshots after stopping writers. Keep the old deployment available to restart until new writes are enabled.
3. Apply final PostgreSQL schema migrations and import these final snapshots. Re-verify counts, relationships, sequences and credentials. The new app roles must own the app-created tables/sequences or receive explicitly scoped grants from the importer; do not leave imported objects accessible only to `doadmin`.
4. Populate app runtime/registry Secrets. Set `apps` to the tested image digests and `postgres_migration_verified = true` in platform tfvars. Plan and apply. Confirm startup/readiness, no SQLite files, and one background scheduler per app.
5. Issue the custom certificates using a CA-side DNS override or the planned cutover issuance window described in the README. Check both applications over the new Tailscale IP with the correct hostnames and root CA.
6. Switch Cloudflare's DNS-only records, verify canonical HTTPS and same-origin write protections, then enable application writes and background syncs. Keep the old app services stopped to prevent divergent writers.
7. Monitor errors, PostgreSQL connections/storage, backups, CA certificate expiry/renewal, Tailscale connectivity and NAT capacity. Verify a subsequent renewal and a gateway restart. Retain the droplet snapshots until a restore has been demonstrated and the new deployment is accepted.

## Rollback boundary

Before the first accepted write in PostgreSQL, stop the new apps and background jobs, restore old DNS, and restart the old services on their frozen SQLite snapshots. After new writes have been accepted, stop writers and reconcile/export those writes before returning to SQLite, or roll back the application image while retaining PostgreSQL. A DNS-only rollback to the frozen SQLite files would discard visibility of new data.

Managed PostgreSQL backups/PITR should be checked in the DigitalOcean account, with independent encrypted logical backups and a documented restore drill. A single DBaaS primary is still a shared failure point for both systems. Terraform state is not a data backup.
