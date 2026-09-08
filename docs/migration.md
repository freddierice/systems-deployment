# Application migration and rollout

Health and Trends use PostgreSQL in development, tests, and production. Each has a versioned `001_initial.sql` and an explicit `python -m <app>.migrate` command. `DATABASE_URL` is required; production also requires verified TLS. Startup checks connectivity and the schema version. Bound parameters, constraints, ID sequences, revision locking, and Health integration state are covered by the PostgreSQL application suites.

## Images

Build from a clean, committed checkout. The temporary rootless BuildKit pod runs in namespace `systems-migration`, behind a default-deny ingress policy. It has no Kubernetes token or host mounts. Delete it after builds. A local Docker-compatible builder can also use each repository's Dockerfile.

```sh
kubectl apply -f kubernetes/buildkit.yaml
kubectl -n systems-migration port-forward pod/buildkitd 1234:1234
# Another terminal; buildctl 0.33.0 or compatible:
scripts/build-image.py health /path/to/health --metadata /protected/health-image.json
scripts/build-image.py trends /path/to/trends --metadata /protected/trends-image.json
```

After both images are pushed, stop the port-forward and delete the temporary namespace with `kubectl delete namespace systems-migration`.

The build script generates a one-hour DOCR push credential and removes its temporary file on exit. The Dockerfiles use a pinned Python base and frozen uv lock. Copy the resulting `@sha256:...` references into `kubernetes/values.production.yaml`. DOCR's Basic registry is `freddierice-systems`, region `nyc3`, with DOKS image-pull integration.

## PostgreSQL validation and schema changes

The production data transfer is complete. Ordinary releases retain the existing databases. Run each app's tests with `TEST_DATABASE_URL` pointing to a disposable PostgreSQL database; the tests isolate their tables in temporary schemas. Never point tests at production.

For fresh provisioning, publish the database secrets, synchronize runtime secrets, and configure workload federation as described in [secrets.md](secrets.md). Initialize each app's database with `python -m <app>.migrate` using its intended image. Confirm `/ready` before enabling traffic and background jobs.

Keep every schema or data migration in a versioned file under `<app>/migrations/`; do not implement new database changes only in `migrate.py`. The release gate compares that directory with the last successfully deployed source revision. Runner-only maintenance, such as retiring the SQLite importer, can deploy without a schema override because neither rollout nor application startup executes the migration runner.

For a release that changes the schema, review the versioned migration and its compatibility with the running image, verify a PostgreSQL backup, and rehearse against a restored disposable database. Execute the migration separately from application startup. The helper `scripts/prepare-migration-pods.py` creates temporary pods from the production values' exact image digests, with database credentials and verified TLS. It does not execute a migration or start background workers. From the administrator's `do-nyc1-systems` context, run the appropriate command only after reviewing the intended image and schema change:

```sh
kubectl --context do-nyc1-systems -n systems exec health-migration -- python -m health.migrate
kubectl --context do-nyc1-systems -n systems exec trends-migration -- python -m trends.migrate
```

Remove the temporary pods after schema verification. Coordinate a maintenance window for schema changes that are incompatible with the running image. Initial schema creation seeds new databases; rerunning the current migration preserves existing rows and identity sequences.

Use managed PostgreSQL backups for recovery and the apps' `scripts/backup.py` commands for custom-format `pg_dump` archives. Verify recovery with `pg_restore` into a disposable database. The protected recovery bucket is `gs://freddie-systems-migration-186933910776`, with uniform bucket-level access and public access prevention. Existing historical snapshots and reports remain preserved there; they contain sensitive app data.

## Rollback

Roll back to a compatible application image while retaining the current PostgreSQL databases. If a schema change prevents image rollback, follow its reviewed recovery procedure and reconcile writes before restoring an older database backup. Preserve cert-manager's account/TLS Secrets and Tailscale Service identity. Application services belong on the cluster; do not restart the retired droplet deployments.

## Updating an app

Test, commit, push and build the app, run any new versioned migrations separately, update its digest, and deploy the production values file. Confirm `/ready`, app routes, and provider configuration through Tailscale. A single replica with `Recreate` prevents duplicate background schedulers. Secret rotations require synchronization and a pod restart for environment-based credentials; Trends' API-key versions are picked up within 60 seconds.
