#!/usr/bin/env python3
"""Install the pinned Gateway API bundle using DOKS's external CRD policy."""
import argparse
import hashlib
import json
import re
import subprocess
import urllib.request

import yaml

VERSION = "v1.6.1"
SHA256 = "24d931f22abd8e40c973264319ead7cfa09d0fb7716b7ab1ee2ff174cb063a73"
URL = f"https://github.com/kubernetes-sigs/gateway-api/releases/download/{VERSION}/standard-install.yaml"


def version_tuple(value):
    if not re.fullmatch(r"v\d+\.\d+\.\d+", value):
        raise ValueError(f"Unrecognized Gateway API version: {value!r}")
    return tuple(map(int, value[1:].split(".")))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--context", required=True)
    args = parser.parse_args()
    kubectl = ["kubectl", "--context", args.context]
    with urllib.request.urlopen(URL, timeout=60) as response:
        bundle = response.read()
    if hashlib.sha256(bundle).hexdigest() != SHA256:
        raise SystemExit("Gateway API bundle checksum mismatch")
    documents = list(yaml.safe_load_all(bundle))
    existing = json.loads(subprocess.check_output(kubectl + ["get", "crd", "-o", "json"]))
    existing = {d["metadata"]["name"]: d for d in existing["items"]}
    for document in documents:
        if document["kind"] != "CustomResourceDefinition":
            continue
        metadata = document["metadata"]
        old = existing.get(metadata["name"])
        if old:
            installed = old["metadata"].get("annotations", {}).get("gateway.networking.k8s.io/bundle-version", "")
            if version_tuple(installed) > version_tuple(VERSION):
                raise SystemExit(f"Refusing to downgrade {metadata['name']} from {installed}")
            retained = {v["name"] for v in document["spec"]["versions"]}
            if set(old.get("status", {}).get("storedVersions", [])) - retained:
                raise SystemExit(f"Migrate stored API versions before upgrading {metadata['name']}")
        # Apply the opt-out and upgraded schema together: the upstream admission
        # policy rejects even metadata changes to an old (<1.5) bundle version.
        metadata.setdefault("annotations", {})["doks.digitalocean.com/install-policy"] = "external"
    subprocess.run(
        kubectl + ["apply", "--server-side", "--force-conflicts", "--field-manager=systems-gateway-api", "-f", "-"],
        input=yaml.safe_dump_all(documents), text=True, check=True,
    )


if __name__ == "__main__":
    main()
