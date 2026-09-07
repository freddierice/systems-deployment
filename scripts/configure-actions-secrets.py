#!/usr/bin/env python3
"""Populate the three Actions credentials in Google Secret Manager.

Run after configure-actions-identity.py. With --apply, the registry publisher
credential is generated from the local doctl session. The deployment credential
comes from the existing systems-digitalocean-token recovery secret. The existing
deployment-repository SSH key is stored for the shared workflow to record image
updates. No personal GitHub login is needed. No credential is printed or passed
on argv.
"""
import argparse
import base64
import json
from pathlib import Path
import subprocess
import sys
import urllib.error
import urllib.request

PROJECT = "macro-events-882dcb"
SECRETS = (
    "systems-actions-digitalocean",
    "systems-actions-digitalocean-deploy",
    "systems-actions-git-key",
)


def command(args, *, payload=None):
    result = subprocess.run(args, input=payload, capture_output=True, check=False)
    if result.returncode:
        raise RuntimeError(f"{args[0]} {args[1]} failed (exit {result.returncode}); no credentials were printed.")
    return result.stdout


def verify_registry_only(token):
    # Generated registry credentials must not grant Kubernetes account access.
    request = urllib.request.Request(
        "https://api.digitalocean.com/v2/kubernetes/clusters",
        headers={"Authorization": "Bearer " + token},
    )
    try:
        with urllib.request.urlopen(request, timeout=30):
            pass
    except urllib.error.HTTPError as error:
        if error.code == 403:
            return
        raise RuntimeError(f"Publisher scope verification returned HTTP {error.code}.") from None
    raise RuntimeError("Generated publisher credential has Kubernetes access; refusing to share it with app builds.")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--apply", action="store_true")
    parser.add_argument("--git-key", type=Path, default=Path("/home/codex/.ssh/systems_deployment"),
                        help="Existing write deploy key for systems-deployment.")
    args = parser.parse_args()
    print("Secret Manager credentials: " + ", ".join(SECRETS))
    if not args.apply:
        print("Dry run. --apply publishes new versions; it never creates a Google service-account key or GitHub Actions secret.")
        return
    git_key = args.git_key.read_bytes()
    if not git_key.startswith(b"-----BEGIN OPENSSH PRIVATE KEY-----"):
        raise RuntimeError("The deployment repository key must be an OpenSSH private key.")
    if args.git_key.stat().st_mode & 0o077:
        raise RuntimeError("The deployment key must not be accessible to other users.")
    for secret in SECRETS:
        command(["gcloud", "secrets", "describe", secret, "--project", PROJECT, "--format=json"])
    deploy_token = command([
        "gcloud", "secrets", "versions", "access", "latest", "--project", PROJECT,
        "--secret", "systems-digitalocean-token",
    ]).strip()
    # doctl registry credentials have registry permissions only. Keep the durable
    # credential in GSM; each hosted build derives a 30-minute registry login.
    config = json.loads(command([
        "doctl", "--context", "freddie-pki", "registry", "docker-config",
        "freddierice-systems", "--read-write", "--expiry-seconds=0",
    ]))
    auth = config["auths"]["registry.digitalocean.com"]
    publisher = base64.b64decode(auth["auth"]).decode().split(":", 1)[1]
    verify_registry_only(publisher)
    values = (publisher.encode(), deploy_token, git_key)
    for secret, value in zip(SECRETS, values):
        if not value:
            raise RuntimeError(f"Refusing to publish an empty credential to {secret}.")
        command([
            "gcloud", "secrets", "versions", "add", secret, "--project", PROJECT,
            "--data-file=-", "--format=value(name)",
        ], payload=value)
        print(f"Published a new version of {secret}.")


if __name__ == "__main__":
    try:
        main()
    except (RuntimeError, OSError, ValueError, KeyError) as error:
        sys.exit(str(error))
