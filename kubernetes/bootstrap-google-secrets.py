#!/usr/bin/env python3
"""Sync runtime secrets from Google without putting values in files or argv."""
import argparse
import json
import os
from pathlib import Path
import re
import subprocess
import sys

import yaml


def read_secret(resource):
    match = re.fullmatch(r"projects/([^/]+)/secrets/([^/]+)/versions/([^/]+)", resource)
    if not match:
        raise ValueError("Expected a full Secret Manager version resource name.")
    project, name, version = match.groups()
    result = subprocess.run(
        ["gcloud", "secrets", "versions", "access", version, f"--project={project}", f"--secret={name}"],
        capture_output=True, text=True,
    )
    if result.returncode:
        raise RuntimeError(f"Cannot access {resource}; check gcloud login and Secret Accessor permission.")
    value = result.stdout.strip()
    if not value:
        raise ValueError(f"Secret {resource} is empty.")
    return value


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--context", required=True)
    parser.add_argument("--operator-only", action="store_true")
    args = parser.parse_args()
    root = Path(__file__).resolve().parents[1]
    mapping = yaml.safe_load((root / "kubernetes/google-secrets.yaml").read_text())
    config = mapping["tailscale"]
    # Read both before writing anything. No value is written to a local file,
    # printed, or passed as a subprocess command-line argument.
    child_env = os.environ.copy()
    child_env["TS_OAUTH_CLIENT_ID"] = read_secret(config["clientID"])
    child_env["TS_OAUTH_CLIENT_SECRET"] = read_secret(config["clientSecret"])
    subprocess.run(["bash", str(root / "scripts/bootstrap-operator.sh"), args.context], env=child_env, check=True)
    if args.operator_only:
        return
    for name, resource in mapping.get("runtime", {}).items():
        values = json.loads(read_secret(resource))
        if not isinstance(values, dict) or not all(isinstance(k, str) and isinstance(v, str) for k, v in values.items()):
            raise ValueError("Runtime secret must be a JSON object of strings")
        manifest = {"apiVersion": "v1", "kind": "Secret", "metadata": {"name": name, "namespace": "systems"}, "type": "Opaque", "stringData": values}
        result = subprocess.run(["kubectl", "--context", args.context, "apply", "--server-side", "--force-conflicts", "--field-manager=systems-secrets", "-f", "-"], input=json.dumps(manifest), text=True, capture_output=True)
        if result.returncode:
            raise RuntimeError("Cannot synchronize " + name)
        print("Synchronized " + name)



if __name__ == "__main__":
    try:
        main()
    except (ValueError, RuntimeError, subprocess.CalledProcessError) as error:
        print(str(error), file=sys.stderr)
        sys.exit(1)
