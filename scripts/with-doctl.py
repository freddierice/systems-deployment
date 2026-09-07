#!/usr/bin/env python3
"""Run a command with the token from an explicit doctl authentication context."""
import argparse
import os
from pathlib import Path
import sys

import yaml


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--context", required=True)
    parser.add_argument("--config", type=Path, default=Path.home() / ".config/doctl/config.yaml")
    parser.add_argument("command", nargs=argparse.REMAINDER)
    args = parser.parse_args()
    command = args.command[1:] if args.command[:1] == ["--"] else args.command
    if not command:
        parser.error("A command is required after --context CONTEXT")
    config = yaml.safe_load(args.config.read_text())
    token = config.get("access-token") if args.context == "default" else config.get("auth-contexts", {}).get(args.context)
    if not isinstance(token, str) or not token.strip():
        sys.exit(f"No DigitalOcean token configured for context {args.context!r}.")
    # Never write the token to tfvars, process arguments, or stdout.
    child_env = os.environ.copy()
    child_env["DIGITALOCEAN_TOKEN"] = token.strip()
    os.execvpe(command[0], command, child_env)


if __name__ == "__main__":
    main()
