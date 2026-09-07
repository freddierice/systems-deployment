#!/usr/bin/env python3
"""Serialize releases across repositories with a generation-conditional GCS lock.

Acquire before opening the Kubernetes firewall; release after firewall cleanup.
GCP_ACCESS_TOKEN comes from GitHub OIDC. The two-hour lease must remain longer
than the entire deployment job timeout, including lock wait and cleanup.
"""
import argparse
import json
import os
from pathlib import Path
import re
import sys
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request

BUCKET = "freddie-systems-actions-186933910776"
OBJECT = "production.lock"
BASE = f"https://storage.googleapis.com/storage/v1/b/{BUCKET}/o/{OBJECT}"
UPLOAD = f"https://storage.googleapis.com/upload/storage/v1/b/{BUCKET}/o"
LEASE_SECONDS = 7200


class APIError(RuntimeError):
    def __init__(self, method, status):
        super().__init__(f"GCS lock {method} failed (HTTP {status or 'unavailable'}).")
        self.status = status


def request(method, url, token, data=None):
    req = urllib.request.Request(
        url, method=method, data=json.dumps(data).encode() if data is not None else None,
        headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json",
                 "Cache-Control": "no-store", "User-Agent": "systems-actions-release-lock"},
    )
    for attempt in range(5):
        try:
            with urllib.request.urlopen(req, timeout=20) as response:
                body = response.read()
                return json.loads(body) if body else None
        except urllib.error.HTTPError as error:
            failure = APIError(method, error.code)
            if error.code != 429 and error.code < 500:
                raise failure from None
        except (urllib.error.URLError, TimeoutError):
            failure = APIError(method, 0)
        if attempt == 4:
            raise failure from None
        time.sleep(2 ** attempt)


def valid_owner(owner):
    if not isinstance(owner, str) or not re.fullmatch(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+/[1-9][0-9]*/[1-9][0-9]*", owner):
        raise ValueError("Lock owner must be repository-owner/repository/run-id/run-attempt using ASCII characters.")
    return owner


def current_lock(token):
    try:
        metadata = request("GET", BASE, token)
        generation = metadata["generation"]
        # Read exactly the current generation. A concurrent replacement forces
        # the caller to retry instead of combining old metadata with new content.
        lock = request("GET", BASE + "?" + urllib.parse.urlencode({
            "alt": "media", "ifGenerationMatch": generation,
        }), token)
    except APIError as error:
        if error.status == 404:
            return None
        raise
    valid_owner(lock.get("owner"))
    if not re.fullmatch(r"[1-9][0-9]*", str(generation)) or type(lock.get("expires_at")) is not int:
        raise ValueError("The stored production lock has invalid lease or generation data.")
    return {**lock, "generation": str(generation)}


def save_state(path, owner, expires_at, generation=None):
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary = tempfile.mkstemp(prefix=".release-lock-", dir=path.parent)
    try:
        with os.fdopen(descriptor, "w") as output:
            json.dump({"bucket": BUCKET, "object": OBJECT, "owner": owner,
                       "expires_at": expires_at, "generation": generation}, output)
            output.write("\n")
            output.flush()
            os.fsync(output.fileno())
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def read_state(path, owner):
    state = json.loads(path.read_text())
    if (state.get("bucket") != BUCKET or state.get("object") != OBJECT
            or state.get("owner") != owner or type(state.get("expires_at")) is not int
            or (state.get("generation") is not None
                and not re.fullmatch(r"[1-9][0-9]*", str(state["generation"])))):
        raise ValueError("Local lock state belongs to another release or is invalid.")
    return state


def acquire(token, path, owner, wait_seconds=1200):
    valid_owner(owner)
    if path.exists():
        read_state(path, owner)
    deadline = time.monotonic() + wait_seconds
    waiting_for = None
    while True:
        try:
            lock = current_lock(token)
            now = int(time.time())
            if lock and lock["owner"] == owner and lock["expires_at"] > now:
                save_state(path, owner, lock["expires_at"], lock["generation"])
                print(f"Production lock acquired by {owner} (generation {lock['generation']}).", flush=True)
                return
            if lock is None or lock["expires_at"] <= now:
                candidate = {"owner": owner, "expires_at": now + LEASE_SECONDS}
                # A pending record lets cleanup recover an accepted upload even
                # if its response is lost before we can record the generation.
                save_state(path, owner, candidate["expires_at"])
                result = request("POST", UPLOAD + "?" + urllib.parse.urlencode({
                    "uploadType": "media", "name": OBJECT,
                    "ifGenerationMatch": lock["generation"] if lock else "0",
                }), token, candidate)
                generation = str(result["generation"])
                if not re.fullmatch(r"[1-9][0-9]*", generation):
                    raise ValueError("GCS did not return a valid lock generation.")
                save_state(path, owner, candidate["expires_at"], generation)
                print(f"Production lock acquired by {owner} (generation {generation}).", flush=True)
                return
            if waiting_for != lock["owner"]:
                waiting_for = lock["owner"]
                print(f"Waiting for production lock held by {waiting_for}.", flush=True)
        except APIError as error:
            # Failed conditional writes and generation races are contention. A
            # retry after a lost upload response also recovers our existing lock.
            if error.status not in (0, 412, 429) and error.status < 500:
                raise
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise RuntimeError("Timed out waiting for the production lock; this release did not proceed.")
        time.sleep(min(5, remaining))


def release(token, path, owner):
    valid_owner(owner)
    if not path.exists():
        print("No local production lock state; nothing to release.")
        return
    state = read_state(path, owner)
    for attempt in range(5):
        try:
            lock = current_lock(token)
            if lock is None or lock["owner"] != owner or (
                state["generation"] is not None and str(state["generation"]) != lock["generation"]
            ):
                print("Production lock is absent or belongs to another acquisition; leaving it unchanged.")
                path.unlink()
                return
            request("DELETE", BASE + "?" + urllib.parse.urlencode({
                "ifGenerationMatch": lock["generation"],
            }), token)
            path.unlink()
            print(f"Production lock released by {owner}.")
            return
        except APIError as error:
            if error.status not in (404, 412):
                raise
            if attempt == 4:
                raise RuntimeError("Production lock kept changing during release; local state retained.") from None


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("operation", choices=["acquire", "release"])
    parser.add_argument("--state", type=Path, help="Defaults to RUNNER_TEMP/systems-release-lock.json.")
    parser.add_argument("--owner", help="Defaults to GITHUB_REPOSITORY/GITHUB_RUN_ID/GITHUB_RUN_ATTEMPT.")
    parser.add_argument("--wait-seconds", type=int, default=1200)
    args = parser.parse_args()
    if args.wait_seconds < 0:
        parser.error("--wait-seconds cannot be negative.")
    owner = args.owner or "/".join(os.environ.get(key, "") for key in (
        "GITHUB_REPOSITORY", "GITHUB_RUN_ID", "GITHUB_RUN_ATTEMPT",
    ))
    valid_owner(owner)
    path = args.state
    if path is None:
        if not os.environ.get("RUNNER_TEMP"):
            parser.error("Set RUNNER_TEMP or provide --state.")
        path = Path(os.environ["RUNNER_TEMP"]) / "systems-release-lock.json"
    token = os.environ.get("GCP_ACCESS_TOKEN", "").strip()
    if args.operation == "release" and not path.exists():
        release(None, path, owner)
        return
    if not token:
        parser.error("GCP_ACCESS_TOKEN is required; refresh it before releasing a long-running job's lock.")
    if args.operation == "acquire":
        acquire(token, path, owner, args.wait_seconds)
    else:
        release(token, path, owner)


if __name__ == "__main__":
    try:
        main()
    except (RuntimeError, ValueError, OSError, KeyError) as error:
        sys.exit(str(error))
