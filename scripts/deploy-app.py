#!/usr/bin/env python3
"""Deploy a trusted main-branch app image without upgrading platform components."""
import argparse
from contextlib import contextmanager
import copy
import fcntl
import http.client
import json
import os
from pathlib import Path
import re
import selectors
import socket
import ssl
import subprocess
import tempfile
import time
import uuid

import yaml

ROOT = Path(__file__).resolve().parents[1]
VALUES = Path("kubernetes/values.production.yaml")
CHART = Path("kubernetes/charts/systems")
CONTEXT = os.environ.get("KUBE_CONTEXT", "do-nyc1-systems")
REGISTRY = "freddierice-systems"
REPOSITORIES = {
    "health": "https://github.com/freddierice/health.git",
    "trends": "https://github.com/freddierice/trends.git",
    "daily-report": "https://github.com/freddierice/time.git",
}
HELM = ["helm", "--kube-context", CONTEXT, "--namespace", "systems"]


class ReleaseError(RuntimeError):
    pass


def run(*arguments, cwd=None, check=True, timeout=120, source_auth=False):
    environment = {**os.environ, "GIT_TERMINAL_PROMPT": "0"}
    if source_auth and environment.get("GH_TOKEN"):
        # gh reads the ephemeral token from its environment. Neither a token URL
        # nor a persisted credential file is needed for the private app clones.
        environment.update({
            "GIT_CONFIG_COUNT": "3",
            "GIT_CONFIG_KEY_0": "credential.https://github.com.helper",
            "GIT_CONFIG_VALUE_0": "",
            "GIT_CONFIG_KEY_1": "credential.https://github.com.helper",
            "GIT_CONFIG_VALUE_1": "!gh auth git-credential",
            # actions/checkout's repository-local header otherwise takes
            # precedence over the token used to read the private app repo.
            "GIT_CONFIG_KEY_2": "http.https://github.com/.extraheader",
            "GIT_CONFIG_VALUE_2": "",
        })
    result = subprocess.run(arguments, cwd=ROOT if cwd is None else cwd, capture_output=True, text=True,
                            timeout=timeout, env=environment)
    if check and result.returncode:
        # Captured CLI output can include credentials or rendered runtime values.
        raise ReleaseError(f"{arguments[0]} {arguments[1]} failed (exit {result.returncode}).")
    return result


def output(*arguments, **kwargs):
    return run(*arguments, **kwargs).stdout.strip()


def validate_inputs(app, source_sha, digest, run_id):
    if app not in REPOSITORIES:
        raise ReleaseError("App must be health, trends, or daily-report.")
    for name, value, pattern in (
        ("source SHA", source_sha, r"[0-9a-f]{40}"),
        ("image digest", digest, r"sha256:[0-9a-f]{64}"),
        ("run ID", run_id, r"[1-9][0-9]{0,19}"),
    ):
        if not re.fullmatch(pattern, value):
            raise ReleaseError(f"Invalid {name}.")


def current_main(app):
    result = output("git", "ls-remote", REPOSITORIES[app], "refs/heads/main", source_auth=True).split()
    if len(result) != 2 or result[1] != "refs/heads/main":
        raise ReleaseError("Could not resolve the application's main branch.")
    return result[0]


def verify_digest(app, source_sha, digest):
    # doctl's list-tags follows API pagination; no Docker credential is generated.
    tags = json.loads(output("doctl", "registry", "repository", "list-tags", app,
                             "--registry", REGISTRY, "--output", "json"))
    # DOCR can also return untagged attestation manifests alongside image tags.
    matches = [tag["manifest_digest"] for tag in tags if tag.get("tag") == source_sha]
    if matches != [digest]:
        raise ReleaseError("The DOCR source-commit tag does not match the requested digest.")


def verify_schema(app, old_sha, new_sha, checkout, schema_verified=False):
    if not isinstance(old_sha, str) or not re.fullmatch(r"[0-9a-f]{40}", old_sha):
        raise ReleaseError("Production sourceRevision is missing; establish a verified baseline first.")
    if schema_verified:
        return  # Explicit operator confirmation; automatic workflows never set this.
    # Health/Trends execute migrations separately, never during image rollout or
    # application startup. Runner maintenance (such as removing the retired
    # SQLite importer) does not change the database required by the new image.
    # Schema/data changes must live in versioned migration files to stay gated.
    paths = ["db.py"] if app == "daily-report" else [f"{app}/migrations"]
    old_tree = output("git", "ls-tree", "-r", old_sha, "--", *paths, cwd=checkout, source_auth=True)
    new_tree = output("git", "ls-tree", "-r", new_sha, "--", *paths, cwd=checkout, source_auth=True)
    if app == "daily-report" and not new_tree:
        # Database-free report revisions neither read nor migrate the retained
        # legacy database. Removing db.py is safe; restoring it stays gated.
        return
    if not old_tree or old_tree != new_tree:
        raise ReleaseError("Migration files changed: follow the database migration runbook, then manually rerun with --schema-verified.")


def resources(manifest):
    indexed = {}
    for item in yaml.safe_load_all(manifest):
        if item is None:
            continue
        key = (item["apiVersion"], item["kind"], item["metadata"].get("namespace", "systems"), item["metadata"]["name"])
        if key in indexed:
            raise ReleaseError(f"Duplicate chart resource: {key[1]}/{key[3]}.")
        indexed[key] = item
    return indexed


def image_only(previous, candidate, app, old_image, new_image):
    """Reject unapplied chart changes, missing resources, and other-app updates."""
    before, after = resources(previous), resources(candidate)
    expected = copy.deepcopy(before)
    key = ("batch/v1", "CronJob", "systems", app) if app == "daily-report" else ("apps/v1", "Deployment", "systems", app)
    if key not in expected:
        raise ReleaseError("The selected app is not part of the existing Helm release.")
    workload = expected[key]["spec"]["jobTemplate"] if app == "daily-report" else expected[key]
    containers = workload["spec"]["template"]["spec"]["containers"]
    target = next((item for item in containers if item["name"] == app), None)
    if target is None or target["image"] not in (old_image, new_image):
        raise ReleaseError("The deployed image differs from the recorded production release.")
    target["image"] = new_image
    if expected != after:
        raise ReleaseError("The chart changes more than the selected app image; reconcile platform/configuration changes separately.")
    return before == after


def verify_replicas(manifest):
    expected = {r["metadata"]["name"]: r["spec"].get("replicas", 1)
                for r in resources(manifest).values() if r["kind"] == "Deployment"}
    live = json.loads(output("kubectl", "--context", CONTEXT, "-n", "systems", "get",
                             "deployments", *sorted(expected), "-o", "json"))
    actual = {r["metadata"]["name"]: r["spec"].get("replicas", 1) for r in live["items"]}
    if actual != expected:
        raise ReleaseError("Live Deployment replicas differ from the chart; reconcile the manual scaling before release.")


def update_values(original, app, image, source_sha):
    """Preserve comments/formatting and touch only this application's two lines."""
    parsed = yaml.safe_load(original)
    previous = parsed["dailyReport"] if app == "daily-report" else parsed["apps"][app]
    expected = copy.deepcopy(parsed)
    selected = expected["dailyReport"] if app == "daily-report" else expected["apps"][app]
    selected.update(image=image, sourceRevision=source_sha)
    section = "dailyReport:" if app == "daily-report" else f"{app}:"
    depth = 0 if app == "daily-report" else 2
    lines, within_app, changed = original.splitlines(keepends=True), False, set()
    for index, line in enumerate(lines):
        if re.match(rf"^{' ' * depth}[a-zA-Z][\w-]*:\s*$", line):
            within_app = line.strip() == section
        elif line.strip() and not line.lstrip().startswith("#") and len(line) - len(line.lstrip()) <= depth:
            within_app = False
        if within_app:
            for key, value in (("image", image), ("sourceRevision", source_sha)):
                if re.match(rf"^{' ' * (depth + 2)}{key}:\s", line):
                    scalar = json.dumps(value) if key == "sourceRevision" else value
                    lines[index] = f"{' ' * (depth + 2)}{key}: {scalar}\n"
                    changed.add(key)
    updated = "".join(lines)
    if changed != {"image", "sourceRevision"} or yaml.safe_load(updated) != expected:
        raise ReleaseError("Production values need explicit image/sourceRevision lines for this app.")
    return updated, previous


def verify_report_schedule(manifest):
    """Do not undo an operator's live suspension or schedule change during release."""
    expected = resources(manifest)["batch/v1", "CronJob", "systems", "daily-report"]["spec"]
    actual = json.loads(output("kubectl", "--context", CONTEXT, "-n", "systems", "get",
                               "cronjob", "daily-report", "-o", "json"))["spec"]
    fields = ("schedule", "timeZone", "suspend", "concurrencyPolicy", "startingDeadlineSeconds",
              "successfulJobsHistoryLimit", "failedJobsHistoryLimit")
    if any(actual.get(field) != expected.get(field) for field in fields):
        raise ReleaseError("Live daily-report scheduling differs from the chart; reconcile it before release.")


def report_smoke(manifest):
    """Start the candidate image with synthetic data and no production access."""
    cron = resources(manifest)["batch/v1", "CronJob", "systems", "daily-report"]
    pod_spec = copy.deepcopy(cron["spec"]["jobTemplate"]["spec"]["template"]["spec"])
    container = next(item for item in pod_spec["containers"] if item["name"] == "daily-report")
    container["command"] = ["python", "-u", "deployment_smoke.py"]
    container["args"] = []
    container.pop("envFrom", None)
    container["env"] = [item for item in container["env"] if item["name"] in (
        "DATA_DIR", "TZ", "PYTHONDONTWRITEBYTECODE", "PYTHONUNBUFFERED")]
    container["volumeMounts"] = [{"name": "data", "mountPath": "/data"}, {"name": "tmp", "mountPath": "/tmp"}]
    pod_spec["containers"] = [container]
    pod_spec.pop("initContainers", None)
    pod_spec.pop("ephemeralContainers", None)
    pod_spec["volumes"] = [{"name": "data", "emptyDir": {}}, {"name": "tmp", "emptyDir": {}}]
    pod_spec["activeDeadlineSeconds"] = 180
    pod_spec["restartPolicy"] = "Never"
    name = "daily-report-smoke-" + uuid.uuid4().hex[:12]
    labels = {"systems.freddie.xyz/release-smoke": name}
    pod = {"apiVersion": "v1", "kind": "Pod", "metadata": {"name": name, "namespace": "systems", "labels": labels},
           "spec": pod_spec}
    # The smoke entry point has no integration calls. Enforce that boundary too,
    # including against accidental network activity during module imports.
    policy = {"apiVersion": "networking.k8s.io/v1", "kind": "NetworkPolicy",
              "metadata": {"name": name, "namespace": "systems"},
              "spec": {"podSelector": {"matchLabels": labels}, "policyTypes": ["Ingress", "Egress"],
                       "ingress": [], "egress": []}}
    with tempfile.TemporaryDirectory(prefix="systems-report-smoke-") as temporary:
        path = Path(temporary) / "smoke.yaml"
        path.write_text(yaml.safe_dump_all([policy, pod]))
        try:
            run("kubectl", "--context", CONTEXT, "-n", "systems", "apply", "--filename", str(path))
            deadline = time.monotonic() + 240
            while time.monotonic() < deadline:
                status = json.loads(output("kubectl", "--context", CONTEXT, "-n", "systems", "get",
                                           "pod", name, "-o", "json", timeout=30))["status"]
                if status.get("phase") == "Succeeded":
                    return
                if status.get("phase") == "Failed":
                    raise ReleaseError("Daily report image failed its isolated container smoke check.")
                time.sleep(2)
            raise ReleaseError("Daily report container smoke check did not finish within 240 seconds.")
        except Exception:
            # This pod has only synthetic data and no integration credentials.
            # Preserve useful failure diagnostics before removing it.
            try:
                logs = run("kubectl", "--context", CONTEXT, "-n", "systems", "logs", name,
                           "--container", "daily-report", "--tail=40", check=False, timeout=30)
                if logs.stdout:
                    print("Daily report smoke output:\n" + logs.stdout, flush=True)
            except (ReleaseError, subprocess.SubprocessError, OSError):
                pass
            raise
        finally:
            # Keep its deny policy in place until the process is gone. If pod
            # cleanup fails, retain the policy and fail the release for cleanup.
            run("kubectl", "--context", CONTEXT, "-n", "systems", "delete", "pod", name,
                "--ignore-not-found", "--wait=true", "--timeout=30s", timeout=45)
            run("kubectl", "--context", CONTEXT, "-n", "systems", "delete", "networkpolicy", name,
                "--ignore-not-found", "--wait=true", "--timeout=30s", timeout=45)


@contextmanager
def gateway_forward():
    """Reach the private gateway through the Kubernetes API, bound to localhost."""
    try:
        process = subprocess.Popen([
            "kubectl", "--context", CONTEXT, "-n", "systems", "port-forward",
            "--address", "127.0.0.1", "service/systems-gateway", ":443",
        ], stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
            text=True, bufsize=1)
    except OSError as error:
        raise ReleaseError("Could not start the gateway port-forward.") from error
    try:
        deadline = time.monotonic() + 30
        with selectors.DefaultSelector() as selector:
            selector.register(process.stdout, selectors.EVENT_READ)
            while time.monotonic() < deadline and process.poll() is None:
                if selector.select(timeout=1):
                    match = re.fullmatch(r"Forwarding from 127\.0\.0\.1:(\d+) -> \d+\s*", process.stdout.readline())
                    if match:
                        yield int(match.group(1))
                        return
        raise ReleaseError("The gateway port-forward did not become ready within 30 seconds.")
    finally:
        if process.poll() is None:
            process.terminate()
        try:
            process.wait(timeout=5)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait(timeout=5)
        process.stdout.close()


def tls_get(hostname, port, path, context):
    connection = http.client.HTTPConnection(hostname, timeout=5)
    with socket.create_connection(("127.0.0.1", port), timeout=5) as transport:
        try:
            connection.sock = context.wrap_socket(transport, server_hostname=hostname)
            connection.request("GET", path, headers={"Host": hostname, "Connection": "close"})
            response = connection.getresponse()
            return response.status, response.getheader("Location")
        finally:
            connection.close()


def smoke(app):
    try:
        context = ssl.create_default_context(cafile=str(ROOT / CHART / "files/root_ca.crt"))
    except OSError as error:
        raise ReleaseError("Could not load the gateway's TLS trust configuration.") from error
    checks = [("/ready", (200, None)), ("/", (200, None))]
    if app == "health":
        checks = [("/ready", (200, None)), ("/", (307, "/workouts")), ("/workouts", (200, None))]
    with gateway_forward() as port:
        for attempt in range(6):
            try:
                if all(tls_get(f"{app}.freddie.xyz", port, path, context) == expected for path, expected in checks):
                    return
            except (OSError, http.client.HTTPException):
                pass
            if attempt < 5:
                time.sleep(3)
    raise ReleaseError("HTTPS readiness or application-page checks failed through the gateway.")


def record_release(app, source_sha, run_id):
    run("git", "add", "--", str(VALUES))
    if not output("git", "diff", "--cached", "--name-only"):
        return
    run("git", "-c", "user.name=systems-deploy", "-c", "user.email=systems-deploy@users.noreply.github.com",
        "commit", "-m", f"Deploy {app} {source_sha[:12]} (build {run_id})")
    baseline = output("git", "rev-parse", "HEAD^")
    for _ in range(3):
        if run("git", "push", "origin", "HEAD:main", check=False).returncode == 0:
            return
        run("git", "fetch", "origin", "main")
        if run("git", "merge-base", "--is-ancestor", "HEAD", "origin/main", check=False).returncode == 0:
            return  # The server accepted the push before its connection failed.
        if output("git", "diff", "--name-only", baseline, "origin/main", "--", str(VALUES), str(CHART)):
            raise ReleaseError("App is healthy, but production configuration changed upstream; reconcile and record the release manually.")
        run("git", "-c", "user.name=systems-deploy", "-c", "user.email=systems-deploy@users.noreply.github.com",
            "rebase", "origin/main")
        baseline = output("git", "rev-parse", "origin/main")
    raise ReleaseError("App is healthy, but recording its release failed; retry this dispatch after fixing Git push access.")


def deploy(args):
    if not args.dry_run:
        if output("git", "status", "--porcelain"):
            raise ReleaseError("Deployment checkout must be clean.")
        run("git", "fetch", "origin", "main")
        run("git", "merge", "--ff-only", "origin/main")
        if output("git", "rev-parse", "HEAD") != output("git", "rev-parse", "origin/main"):
            raise ReleaseError("Deployment checkout must be exactly origin/main.")
    if current_main(args.app) != args.source_sha:
        print(f"Skipped stale {args.app} release: source commit is no longer main.")
        return
    verify_digest(args.app, args.source_sha, args.image_digest)
    image = f"registry.digitalocean.com/{REGISTRY}/{args.app}@{args.image_digest}"
    path = ROOT / VALUES
    updated, previous = update_values(path.read_text(), args.app, image, args.source_sha)
    with tempfile.TemporaryDirectory(prefix="systems-release-") as temporary:
        checkout = Path(temporary) / "source.git"
        run("git", "clone", "--bare", "--filter=blob:none", "--single-branch", "--branch", "main",
            REPOSITORIES[args.app], str(checkout), source_auth=True)
        verify_schema(args.app, previous.get("sourceRevision"), args.source_sha, checkout,
                      schema_verified=args.schema_verified)
        candidate_values = Path(temporary) / "values.yaml"
        candidate_values.write_text(updated)
        candidate = output("helm", "template", "systems", str(ROOT / CHART), "--namespace", "systems",
                           "--values", str(candidate_values))
        release = json.loads(output(*HELM, "status", "systems", "--output", "json"))
        if release["info"]["status"] != "deployed":
            raise ReleaseError("The existing Helm release must be healthy and deployed before an app release.")
        unchanged = image_only(output(*HELM, "get", "manifest", "systems"), candidate,
                               args.app, previous["image"], image)
        verify_replicas(candidate)
        if args.app == "daily-report":
            verify_report_schedule(candidate)
        if args.dry_run:
            print(f"Validated {args.app} release; dry run made no cluster or production-state changes.")
            return
        if current_main(args.app) != args.source_sha:
            print(f"Skipped stale {args.app} release: main advanced during validation.")
            return
        if args.app == "daily-report":
            # Validate before changing the CronJob; current Jobs continue using
            # their existing image and no report/print invocation is created.
            report_smoke(candidate)
            verify_report_schedule(candidate)
            if current_main(args.app) != args.source_sha:
                print("Skipped stale daily-report release: main advanced during the smoke check.")
                return
        if not unchanged:
            print(f"Deploying {args.app} {args.source_sha[:12]} at {args.image_digest}.", flush=True)
            run(*HELM, "upgrade", "--install", "systems", str(ROOT / CHART), "--values", str(candidate_values),
                "--atomic", "--wait", "--timeout", "10m", timeout=1320)
        try:
            if args.app != "daily-report":
                smoke(args.app)
        except ReleaseError:
            if not unchanged:
                run(*HELM, "rollback", "systems", str(release["version"]), "--wait", "--timeout", "10m", timeout=660)
            raise
    path.write_text(updated)
    record_release(args.app, args.source_sha, args.run_id)
    print(f"Healthy {args.app} release recorded in production values.")


def main():
    global CONTEXT, HELM
    parser = argparse.ArgumentParser(description=__doc__)
    for flag in ("app", "source-sha", "image-digest", "run-id"):
        parser.add_argument(f"--{flag}", required=True)
    parser.add_argument("--dry-run", action="store_true", help="Validate current local configuration without deployment or Git commits.")
    parser.add_argument("--schema-verified", action="store_true",
                        help="Operator only: required migrations were reviewed and executed following the runbook; never set in automatic workflows.")
    parser.add_argument("--context", default=CONTEXT, help="Kubernetes context (default: KUBE_CONTEXT or do-nyc1-systems).")
    args = parser.parse_args()
    try:
        validate_inputs(args.app, args.source_sha, args.image_digest, args.run_id)
        CONTEXT = args.context
        HELM = ["helm", "--kube-context", CONTEXT, "--namespace", "systems"]
        lock_path = Path.home() / ".local/state/systems-deploy/release.lock"
        lock_path.parent.mkdir(parents=True, exist_ok=True)
        with lock_path.open("a") as lock:
            fcntl.flock(lock, fcntl.LOCK_EX)
            deploy(args)
    except (ReleaseError, subprocess.TimeoutExpired, OSError, ValueError, KeyError) as error:
        parser.exit(1, f"Release stopped: {error}\n")


if __name__ == "__main__":
    main()
