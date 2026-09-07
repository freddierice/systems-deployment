#!/usr/bin/env python3
"""Configure app grants inside DOKS, verify isolation, then sync runtime Secrets.

Reads sensitive Terraform output directly into memory. No passwords are printed,
passed on the command line, or written to intermediate files.
"""

import argparse
import base64
import json
from pathlib import Path
import subprocess
import sys
from urllib.parse import quote
import uuid


def database_url(config, app):
    return (
        f"postgresql://{quote(app['user'], safe='')}:{quote(app['password'], safe='')}"
        f"@{config['host']}:{config['port']}/{quote(app['database'], safe='')}"
        "?sslmode=verify-full&sslrootcert=%2Fetc%2Fpostgresql%2Fca.crt"
    )


def secret(name, data):
    return {
        "apiVersion": "v1", "kind": "Secret",
        "metadata": {"name": name, "namespace": "systems"},
        "type": "Opaque",
        "data": {key: base64.b64encode(value.encode()).decode() for key, value in data.items()},
    }


def grant_script():
    commands = []
    checks = []
    for app, other in [("health", "trends"), ("trends", "health")]:
        commands.append(f"""
psql --dbname=defaultdb --set=ON_ERROR_STOP=1 <<'SQL'
BEGIN;
REVOKE ALL ON DATABASE {app} FROM PUBLIC;
REVOKE ALL ON DATABASE {app} FROM {other};
GRANT CONNECT, TEMPORARY ON DATABASE {app} TO {app};
COMMIT;
SQL
psql --dbname={app} --set=ON_ERROR_STOP=1 <<'SQL'
BEGIN;
REVOKE ALL ON SCHEMA public FROM PUBLIC;
REVOKE ALL ON SCHEMA public FROM {other};
GRANT USAGE, CREATE ON SCHEMA public TO {app};
COMMIT;
SQL
""")
        checks.append(f"""
PGUSER={app} PGPASSWORD="${{{app.upper()}_PASSWORD}}" psql --dbname={app} --set=ON_ERROR_STOP=1 -c 'SELECT 1' >/dev/null
if PGUSER={app} PGPASSWORD="${{{app.upper()}_PASSWORD}}" psql --dbname={other} --set=ON_ERROR_STOP=1 -c 'SELECT 1' >/dev/null 2>&1; then
  echo 'Database isolation failed: {app} can connect to {other}' >&2
  exit 1
fi
""")
    # Restrict BOTH databases before checking either login's cross-access.
    return "set -eu\n" + "\n".join(commands + checks) + "\necho 'Database grants and cross-database isolation verified.'\n"


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--context", required=True, help="Explicit systems kubeconfig context")
    parser.add_argument("--infra", default=str(Path(__file__).resolve().parents[1] / "infra"))
    args = parser.parse_args()
    config = json.loads(subprocess.check_output(
        ["terraform", f"-chdir={args.infra}", "output", "-json", "database_bootstrap"]
    ))
    if set(config["apps"]) != {"health", "trends"} or any(
        app["database"] != name or app["user"] != name
        for name, app in config["apps"].items()
    ):
        raise ValueError("Bootstrap expects the health and trends database/role pairs.")
    if not config["host"].startswith("private-"):
        raise ValueError("Refusing a PostgreSQL host without the DigitalOcean private- prefix.")
    ca = config["ca"]
    # DigitalOcean provider returns the CA base64-encoded.
    if "BEGIN CERTIFICATE" not in ca:
        ca = base64.b64decode(ca, validate=True).decode()
    if "BEGIN CERTIFICATE" not in ca:
        raise ValueError("Terraform output did not include a PEM PostgreSQL CA.")

    def kubectl(*arguments, manifest=None, quiet=False):
        result = subprocess.run(
            ["kubectl", "--context", args.context, *arguments],
            input=json.dumps(manifest) if manifest is not None else None,
            text=True, capture_output=True,
        )
        if result.returncode:
            # API validation errors can echo Secret values; do not print them.
            raise RuntimeError(f"kubectl {arguments[0]} failed; inspect resource status with kubectl.")
        if not quiet and result.stdout:
            print(result.stdout.strip())

    def apply(manifest):
        kubectl("apply", "--server-side", "--field-manager=systems-bootstrap", "-f", "-", manifest=manifest)

    apply({"apiVersion": "v1", "kind": "Namespace", "metadata": {"name": "systems"}})
    apply({"apiVersion": "v1", "kind": "ConfigMap", "metadata": {"name": "postgres-ca", "namespace": "systems"}, "data": {"ca.crt": ca}})

    job_name = "database-bootstrap-" + uuid.uuid4().hex[:10]
    apply(secret(job_name, {
        "PGUSER": config["admin"], "PGPASSWORD": config["password"],
        "HEALTH_PASSWORD": config["apps"]["health"]["password"],
        "TRENDS_PASSWORD": config["apps"]["trends"]["password"],
    }))
    try:
        apply({
            "apiVersion": "batch/v1", "kind": "Job",
            "metadata": {"name": job_name, "namespace": "systems"},
            "spec": {
                "backoffLimit": 0, "activeDeadlineSeconds": 240, "ttlSecondsAfterFinished": 600,
                "template": {"spec": {
                    "restartPolicy": "Never", "automountServiceAccountToken": False,
                    "securityContext": {"runAsNonRoot": True, "runAsUser": 1000, "runAsGroup": 1000, "seccompProfile": {"type": "RuntimeDefault"}},
                    "containers": [{
                        "name": "grants", "image": "postgres:17-alpine",
                        "command": ["sh", "-ec", grant_script()],
                        "envFrom": [{"secretRef": {"name": job_name}}],
                        "env": [{"name": key, "value": str(value)} for key, value in {
                            "PGHOST": config["host"], "PGPORT": config["port"],
                            "PGSSLMODE": "verify-full", "PGSSLROOTCERT": "/etc/postgresql/ca.crt",
                            "PGCONNECT_TIMEOUT": "10",
                        }.items()],
                        "securityContext": {"allowPrivilegeEscalation": False, "readOnlyRootFilesystem": True, "capabilities": {"drop": ["ALL"]}},
                        "resources": {"requests": {"cpu": "50m", "memory": "32Mi"}, "limits": {"memory": "128Mi"}},
                        "volumeMounts": [{"name": "ca", "mountPath": "/etc/postgresql", "readOnly": True}, {"name": "tmp", "mountPath": "/tmp"}],
                    }],
                    "volumes": [{"name": "ca", "configMap": {"name": "postgres-ca"}}, {"name": "tmp", "emptyDir": {}}],
                }},
            },
        })
        print(f"Waiting for systems/{job_name}; this can take up to four minutes.")
        kubectl("-n", "systems", "wait", f"job/{job_name}", "--for=condition=complete", "--timeout=250s")
        for name, app in config["apps"].items():
            apply(secret(f"{name}-database", {"DATABASE_URL": database_url(config, app)}))
        print("Database grants verified and application Secrets synchronized. Restart running apps after rotating credentials.")
    finally:
        # Remove the admin credential even on failure. Logs contain no data/passwords.
        try:
            kubectl("-n", "systems", "logs", f"job/{job_name}", quiet=False)
        finally:
            try:
                kubectl("-n", "systems", "delete", "job", job_name, "--ignore-not-found", "--wait=false", quiet=True)
            finally:
                kubectl("-n", "systems", "delete", "secret", job_name, "--ignore-not-found", quiet=True)


if __name__ == "__main__":
    try:
        main()
    except (ValueError, RuntimeError, subprocess.CalledProcessError) as error:
        print(f"Bootstrap failed: {error}", file=sys.stderr)
        sys.exit(1)
