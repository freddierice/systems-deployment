#!/usr/bin/env python3
"""Temporarily allow this hosted runner's IPv4 address through the DOKS firewall.

Run prepare before deployment and cleanup in an always() step, sharing --state.
The DigitalOcean token is read only from DIGITALOCEAN_ACCESS_TOKEN. Updates replace
the address list, so deployment jobs must be serialized. DigitalOcean does not
document conditional cluster updates: do not apply Terraform/firewall edits
concurrently with a release.
"""

import argparse
import ipaddress
import json
import os
from pathlib import Path
import socket
import sys
import time
import urllib.error
import urllib.parse
import urllib.request


CLUSTER_ID = "27e1393e-6d47-48b4-8b84-5df941e60fd4"
CLUSTER_URL = f"https://api.digitalocean.com/v2/kubernetes/clusters/{CLUSTER_ID}"


class RetryableError(RuntimeError):
    def __init__(self, message, delay=2):
        super().__init__(message)
        self.delay = delay


def request(method, token, data=None):
    req = urllib.request.Request(
        CLUSTER_URL, method=method,
        data=json.dumps(data).encode() if data is not None else None,
        headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json",
                 "User-Agent": "systems-actions-cluster-access"},
    )
    try:
        with urllib.request.urlopen(req, timeout=20) as response:
            return json.load(response)["kubernetes_cluster"]
    except urllib.error.HTTPError as error:
        message = f"DigitalOcean cluster {method} returned HTTP {error.code}."
        if error.code == 429 or error.code >= 500:
            delay = error.headers.get("Retry-After", "2")
            raise RetryableError(message, min(30, max(1, int(delay))) if delay.isdigit() else 2) from None
        raise RuntimeError(message) from None
    except (urllib.error.URLError, TimeoutError):
        raise RetryableError(f"DigitalOcean cluster {method} could not complete; will reread its state.") from None


def get_cluster(token):
    for attempt in range(5):
        try:
            cluster = request("GET", token)
            if cluster.get("id") != CLUSTER_ID:
                raise RuntimeError("DigitalOcean returned an unexpected cluster ID.")
            return cluster
        except RetryableError as error:
            if attempt == 4:
                raise
            time.sleep(max(error.delay, 2 ** attempt))


def addresses(cluster):
    firewall = cluster.get("control_plane_firewall") or {}
    if firewall.get("enabled") is not True:
        raise RuntimeError("The existing control-plane firewall must already be enabled.")
    result = firewall.get("allowed_addresses")
    if not isinstance(result, list) or not all(isinstance(address, str) for address in result):
        raise RuntimeError("DigitalOcean returned an invalid firewall address list.")
    return result


def runner_cidr(value=None):
    if value is None:
        with urllib.request.urlopen("https://api.ipify.org", timeout=20) as response:
            value = response.read(128).decode().strip()
    ip = ipaddress.ip_address(value)
    if ip.version != 4 or not ip.is_global or ip.is_multicast:
        raise ValueError("The runner address must be one public IPv4 address.")
    return f"{ip}/32"


def contains_cidr(allowed, cidr):
    network = ipaddress.ip_network(cidr)
    return any(ipaddress.ip_network(address, strict=False) == network for address in allowed)


def save_state(path, state):
    path.parent.mkdir(parents=True, exist_ok=True)
    # Exclusive creation avoids overwriting another prepare's cleanup ownership.
    with os.fdopen(os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600), "w") as output:
        json.dump(state, output)
        output.write("\n")
        output.flush()
        os.fsync(output.fileno())


def read_state(path):
    state = json.loads(path.read_text())
    if (state.get("cluster_id") != CLUSTER_ID or type(state.get("added")) is not bool
            or not isinstance(state.get("cidr"), str)
            or state.get("cidr") != runner_cidr(state.get("cidr", "").removesuffix("/32"))):
        raise ValueError("Cleanup state is invalid or belongs to another cluster.")
    return state


def update_address(token, cidr, adding, dry_run=False):
    for attempt in range(5):
        # Reread before every replacement, including retries and cleanup, to
        # preserve addresses changed by administrators since prepare.
        cluster = get_cluster(token)
        allowed = addresses(cluster)
        present = contains_cidr(allowed, cidr)
        if adding and present:
            return cluster
        # Cleanup submits a later PUT even if the first GET shows no address:
        # an interrupted prepare may have an accepted addition still propagating.
        desired = allowed + [cidr] if adding else [
            address for address in allowed
            if ipaddress.ip_network(address, strict=False) != ipaddress.ip_network(cidr)
        ]
        if len(desired) > 500:
            raise RuntimeError("The DOKS firewall has reached its 500-address limit.")
        if dry_run:
            print(f"Would {'add' if adding else 'remove'} {cidr}; retain {len(allowed) - int(present)} other addresses.")
            return cluster
        try:
            return request("PUT", token, {
                "control_plane_firewall": {"enabled": True, "allowed_addresses": desired},
            })
        except RetryableError as error:
            if attempt == 4:
                raise
            time.sleep(max(error.delay, 2 ** attempt))


def wait_for_change(token, cidr, present, timeout):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        cluster = get_cluster(token)
        if (contains_cidr(addresses(cluster), cidr) == present
                and cluster.get("status", {}).get("state") == "running"):
            if not present:
                return
            endpoint = urllib.parse.urlsplit(cluster.get("endpoint", ""))
            if endpoint.scheme != "https" or not endpoint.hostname:
                raise RuntimeError("DigitalOcean returned an invalid Kubernetes API endpoint.")
            try:
                # Confirm new TCP connections can reach the API. kubectl performs
                # authenticated readiness checks after obtaining its kubeconfig.
                with socket.create_connection((endpoint.hostname, endpoint.port or 443), timeout=5):
                    return
            except OSError:
                pass
        time.sleep(min(3, max(0, deadline - time.monotonic())))
    raise RuntimeError(f"Timed out waiting for firewall {'access' if present else 'cleanup'} for {cidr}; cleanup state retained.")


def prepare(token, path, ip=None, dry_run=False, timeout=180):
    cidr = runner_cidr(ip)
    # Preserve the address in Actions logs even if the runner disappears before
    # cleanup, so a stranded rule can be identified without its temporary disk.
    print(f"Runner public IPv4 allowance: {cidr}", flush=True)
    if path.exists():
        state = read_state(path)
        if state["cidr"] != cidr:
            raise RuntimeError("State belongs to another runner address; clean it up before preparing access.")
    else:
        state = {"cluster_id": CLUSTER_ID, "cidr": cidr,
                 "added": not contains_cidr(addresses(get_cluster(token)), cidr)}
        if not dry_run:
            # Persist before PUT so an interrupted/failed prepare is cleanable.
            save_state(path, state)
    if state["added"]:
        update_address(token, cidr, adding=True, dry_run=dry_run)
    if not dry_run:
        wait_for_change(token, cidr, present=True, timeout=timeout)
        print(f"Kubernetes API reachable; {'temporarily added' if state['added'] else 'retained existing'} {cidr}.")


def cleanup(token, path, dry_run=False, timeout=180):
    if not path.exists():
        print("No runner firewall state exists; no cleanup needed.")
        return
    state = read_state(path)
    if state["added"]:
        update_address(token, state["cidr"], adding=False, dry_run=dry_run)
        if not dry_run:
            wait_for_change(token, state["cidr"], present=False, timeout=timeout)
    if not dry_run:
        path.unlink()
        print(f"Firewall cleanup complete; {'removed temporary' if state['added'] else 'retained pre-existing'} {state['cidr']}.")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("operation", choices=["prepare", "cleanup"])
    parser.add_argument("--state", type=Path, help="State file shared by prepare and cleanup; defaults under RUNNER_TEMP.")
    parser.add_argument("--ip", help="Use this public IPv4 instead of discovering it with api.ipify.org.")
    parser.add_argument("--timeout", type=int, default=180, help="Seconds to wait for each firewall change (default: 180).")
    parser.add_argument("--dry-run", action="store_true", help="Read and report proposed changes without writing state or firewall rules.")
    args = parser.parse_args()
    if args.timeout <= 0 or (args.operation == "cleanup" and args.ip):
        parser.error("--timeout must be positive; --ip is supported only by prepare.")
    path = args.state
    if path is None:
        runner_temp = os.environ.get("RUNNER_TEMP")
        if not runner_temp:
            parser.error("Set RUNNER_TEMP or provide --state.")
        path = Path(runner_temp) / "systems-doks-firewall.json"
    if args.operation == "cleanup" and not path.exists():
        cleanup(None, path)
        return
    token = os.environ.get("DIGITALOCEAN_ACCESS_TOKEN", "").strip()
    if not token:
        parser.error("DIGITALOCEAN_ACCESS_TOKEN is required.")
    if args.operation == "prepare":
        prepare(token, path, args.ip, args.dry_run, args.timeout)
    else:
        cleanup(token, path, args.dry_run, args.timeout)


if __name__ == "__main__":
    try:
        main()
    except (RuntimeError, ValueError, OSError) as error:
        sys.exit(str(error))
