# Application migration and rollout

Health and Trends retain SQLite for local development and use PostgreSQL in their production containers. Each has a versioned `001_initial.sql`, explicit `python -m <app>.migrate` command, and `--import-sqlite` option. Startup checks the schema version; it never creates a SQLite fallback in production. Bound parameters, integer flags, ID sequences, revision locking, and Health integration state are covered by application tests.

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

## Final transfer

1. Pass both apps' SQLite and PostgreSQL suites, and rehearse a consistent snapshot import into disposable PostgreSQL. The importer requires matching table sets/schema version, checks SQLite integrity and foreign keys, refuses populated targets, copies all tables with constraints enabled, checks row counts and SHA-256 hashes, and resets generated sequences. Everything commits atomically. Monetary values stay exact text; Health measurements use double precision to preserve SQLite values.
2. Run `scripts/publish-database-secrets.py`, then `kubernetes/bootstrap-google-secrets.py --context do-nyc1-systems`. The one-time legacy runtime import is described in [secrets.md](secrets.md). Configure workload federation and apply its public ConfigMap.
3. Set production image digests and enable the app flags with background jobs disabled. `scripts/prepare-migration-pods.py` creates temporary pods from these exact images without starting app processes. Validate private, TLS-verified database access and Trends secret access from these pods. The destination app databases must be empty.
4. Stop both old writers: `systemctl --user stop health.service trends.service`. Use `scripts/snapshot-sqlite.py SOURCE DESTINATION` for each final snapshot. Preserve the snapshots, old code revisions and systemd configuration, and upload the snapshots to the private migration backup bucket before import.
5. Stream each snapshot into its migration pod's memory-backed `/migration` directory, then execute `python -m <app>.migrate --import-sqlite /migration/<app>.sqlite3`. Preserve the count/hash reports with the backups. Do not rerun against populated destinations.
6. Run `kubernetes/deploy.sh do-nyc1-systems kubernetes/values.production.yaml`. Confirm both `/ready` probes, app routes, data counts, and settings/provider configuration before accepting traffic. Production requires `sslmode=verify-full`, non-root users, read-only container roots, and immutable image references.
7. Run `scripts/app-dns.py` to confirm the two existing DNS-only A records, then `scripts/app-dns.py --switch-to 100.91.90.6`. cert-manager automatically retries Gateway API HTTP-01 issuance after DNS propagation. Wait for both Certificates to become Ready; use `cmctl renew --context do-nyc1-systems -n systems health-tls trends-tls` to exercise renewal when needed. Verify both certificates with the Freddie root and actual HTTPS requests through Tailscale. Old DNS caches can take several minutes to expire.
8. Enable `backgroundJobsEnabled` and deploy again. Confirm Health's provider sync and Trends' provider workers. Delete migration pods and the temporary builder/test namespace. Keep the old services stopped to avoid divergent writers.

The protected backup bucket is `gs://freddie-systems-migration-186933910776`, with uniform bucket-level access and public access prevention. It contains sensitive app data; grant access only to migration administrators. Google encrypts stored objects. Keep the frozen SQLite backup for recovery, and use managed PostgreSQL backups for subsequent changes.

## Rollback

Before PostgreSQL accepts new writes, stop new apps/background jobs, restore DNS with `scripts/app-dns.py --switch-to 100.66.233.125`, and restart the frozen droplet services. Once PostgreSQL has accepted writes, roll back the application image while retaining PostgreSQL, or first reconcile/export new data. Switching DNS back to stale SQLite after new writes would lose visibility of those changes. Preserve cert-manager's account/TLS Secrets and Tailscale Service identity.

## Updating an app

Test, commit, push and build the app, run any new versioned migrations as a separate Job, update its digest, and deploy the production values file. Do not use the initial SQLite import for ordinary deployments. A single replica with `Recreate` prevents duplicate background schedulers. Secret rotations require synchronization and a pod restart for environment-based credentials; Trends' API-key versions are picked up within 60 seconds.
