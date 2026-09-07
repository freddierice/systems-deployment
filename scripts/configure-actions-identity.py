#!/usr/bin/env python3
"""Plan GitHub OIDC identity setup; use --apply to provision it without secret values.

Requires authenticated gh and gcloud CLIs. Existing matching resources and IAM
bindings are retained. Conflicting trust configuration is never overwritten.
"""

import argparse
import json
import re
import subprocess

PROJECT = "macro-events-882dcb"
PROJECT_NUMBER = "186933910776"
OWNER_ID = "2191702"
POOL = "systems-actions"
PROVIDER = "github"
ISSUER = "https://token.actions.githubusercontent.com"
PROVIDER_RESOURCE = (
    f"projects/{PROJECT_NUMBER}/locations/global/workloadIdentityPools/{POOL}/providers/{PROVIDER}"
)
TARGETS = {
    "health": {
        "repository": "freddierice/health",
        "workflow": "release.yml",
        "event": "push",
        "secrets": ("systems-actions-digitalocean", "systems-actions-github-dispatch"),
    },
    "trends": {
        "repository": "freddierice/trends",
        "workflow": "release.yml",
        "event": "push",
        "secrets": ("systems-actions-digitalocean", "systems-actions-github-dispatch"),
    },
    "deploy": {
        "repository": "freddierice/systems-deployment",
        "workflow": "deploy.yml",
        "event": "repository_dispatch",
        "secrets": ("systems-actions-digitalocean-deploy", "systems-actions-github-dispatch"),
    },
}
MAPPING = {"google.subject": "assertion.sub"} | {
    f"attribute.{claim}": f"assertion.{claim}"
    for claim in (
        "repository_id", "repository_owner_id", "repository", "ref",
        "event_name", "workflow_ref", "runner_environment",
    )
}


class ConfigurationError(RuntimeError):
    pass


def command(*arguments, allow_missing=False, payload=None):
    """Capture CLI output; never retrieve credentials or print response bodies."""
    try:
        result = subprocess.run(
            arguments,
            input=json.dumps(payload) if payload is not None else None,
            text=True,
            capture_output=True,
            timeout=180,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired) as error:
        raise ConfigurationError(f"Could not run {arguments[0]} ({type(error).__name__}).") from None
    if result.returncode:
        missing = re.search(r"\bNOT_FOUND\b|\(HTTP 404\)", result.stderr)
        if allow_missing and missing:
            return None
        raise ConfigurationError(
            f"{arguments[0]} {' '.join(arguments[1:3])} failed (exit {result.returncode}); "
            "check CLI authentication and resource permissions."
        )
    return result.stdout


def document(*arguments, allow_missing=False):
    output = command(*arguments, allow_missing=allow_missing)
    if output is None:
        return None
    try:
        return json.loads(output)
    except ValueError:
        raise ConfigurationError(f"{arguments[0]} returned invalid JSON.") from None


def repository_ids():
    command("gh", "auth", "status", "--hostname", "github.com")
    found = {}
    for name, target in TARGETS.items():
        repository = target["repository"]
        data = document("gh", "api", f"repos/{repository}")
        identifier = data.get("id")
        if (
            data.get("full_name") != repository
            or str(data.get("owner", {}).get("id")) != OWNER_ID
            or data.get("default_branch") != "main"
            or type(identifier) is not int
            or identifier <= 0
        ):
            raise ConfigurationError(f"Unexpected repository identity or default branch for {repository}.")
        found[name] = str(identifier)
    if found["deploy"] != "1360448012":
        raise ConfigurationError("The deployment repository's immutable GitHub ID changed.")
    return found


def validate_google_project():
    data = document("gcloud", "projects", "describe", PROJECT, "--format=json")
    if data.get("projectId") != PROJECT or str(data.get("projectNumber")) != PROJECT_NUMBER:
        raise ConfigurationError("Google project identity did not match the expected project.")


def resource_plan(identifiers):
    if set(identifiers) != set(TARGETS) or any(
        not re.fullmatch(r"[1-9][0-9]*", value) for value in identifiers.values()
    ):
        raise ConfigurationError("Every repository must have its numeric GitHub ID.")
    if len(set(identifiers.values())) != len(TARGETS):
        raise ConfigurationError("Repository identities must be distinct.")
    clauses = []
    accounts = []
    for name, target in TARGETS.items():
        repository = target["repository"]
        identifier = identifiers[name]
        workflow = f"{repository}/.github/workflows/{target['workflow']}@refs/heads/main"
        clauses.append(
            f"(assertion.repository_id == '{identifier}' && "
            f"assertion.repository == '{repository}' && "
            f"assertion.event_name == '{target['event']}' && "
            f"assertion.workflow_ref == '{workflow}')"
        )
        account = f"systems-ci-{name}"
        accounts.append({
            "name": account,
            "email": f"{account}@{PROJECT}.iam.gserviceaccount.com",
            "repository": repository,
            "repository_id": identifier,
            "principal": (
                f"principalSet://iam.googleapis.com/projects/{PROJECT_NUMBER}/locations/global/"
                f"workloadIdentityPools/{POOL}/attribute.repository_id/{identifier}"
            ),
            "secrets": list(target["secrets"]),
        })
    condition = (
        f"assertion.repository_owner_id == '{OWNER_ID}' && "
        "assertion.ref == 'refs/heads/main' && "
        "assertion.runner_environment == 'github-hosted' && (" + " || ".join(clauses) + ")"
    )
    return {
        "project": PROJECT,
        "pool": POOL,
        "provider": PROVIDER_RESOURCE,
        "attribute_mapping": MAPPING,
        "attribute_condition": condition,
        "service_accounts": accounts,
        "empty_secrets_if_absent": sorted({secret for target in TARGETS.values() for secret in target["secrets"]}),
        "repository_variables": ["GCP_WORKLOAD_IDENTITY_PROVIDER", "GCP_SERVICE_ACCOUNT"],
    }


def ensure_enabled(data, resource):
    if data.get("disabled") or data.get("state", "ACTIVE") != "ACTIVE":
        raise ConfigurationError(f"Existing {resource} is disabled or deleted; refusing to change it.")


def ensure_provider(plan):
    pool_flags = ("--location=global", f"--project={PROJECT}")
    pool = document("gcloud", "iam", "workload-identity-pools", "describe", POOL,
                    *pool_flags, "--format=json", allow_missing=True)
    if pool is None:
        command("gcloud", "iam", "workload-identity-pools", "create", POOL,
                *pool_flags, "--display-name=Systems GitHub Actions", "--quiet")
    else:
        ensure_enabled(pool, "workload identity pool")
    flags = (f"--workload-identity-pool={POOL}", *pool_flags)
    providers = document("gcloud", "iam", "workload-identity-pools", "providers", "list",
                         *flags, "--format=json")
    if any(item["name"].rsplit("/", 1)[-1] != PROVIDER for item in providers):
        raise ConfigurationError("The dedicated pool contains another provider; refusing to share its trust.")
    existing = next(iter(providers), None)
    if existing is None:
        command(
            "gcloud", "iam", "workload-identity-pools", "providers", "create-oidc", PROVIDER,
            *flags, f"--issuer-uri={ISSUER}",
            "--attribute-mapping=" + ",".join(f"{key}={value}" for key, value in MAPPING.items()),
            "--attribute-condition=" + plan["attribute_condition"], "--quiet",
        )
        return
    ensure_enabled(existing, "workload identity provider")
    oidc = existing.get("oidc", {})
    if (
        oidc.get("issuerUri", "").rstrip("/") != ISSUER
        or oidc.get("allowedAudiences", [])
        or existing.get("attributeMapping") != MAPPING
        or existing.get("attributeCondition") != plan["attribute_condition"]
    ):
        raise ConfigurationError("Existing GitHub provider trust differs from this plan; refusing to overwrite it.")


def ensure_binding(prefix, resource, member, role):
    policy = document(*prefix, "get-iam-policy", resource, f"--project={PROJECT}", "--format=json")
    if any(
        binding.get("role") == role
        and member in binding.get("members", [])
        and not binding.get("condition")
        for binding in policy.get("bindings", [])
    ):
        return
    command(*prefix, "add-iam-policy-binding", resource, f"--project={PROJECT}",
            f"--member={member}", f"--role={role}",
            "--condition=None", "--quiet")


def set_repository_variable(repository, name, value):
    endpoint = f"repos/{repository}/actions/variables"
    existing = document("gh", "api", f"{endpoint}/{name}", allow_missing=True)
    if existing is not None and existing.get("value") == value:
        return
    method = "POST" if existing is None else "PATCH"
    destination = endpoint if existing is None else f"{endpoint}/{name}"
    command("gh", "api", "--method", method, destination, "--input", "-",
            payload={"name": name, "value": value})


def apply_plan(plan):
    project_flag = f"--project={PROJECT}"
    command("gcloud", "services", "enable", "iam.googleapis.com", "iamcredentials.googleapis.com",
            "sts.googleapis.com", "secretmanager.googleapis.com", project_flag, "--quiet")
    ensure_provider(plan)
    for secret in plan["empty_secrets_if_absent"]:
        existing = document("gcloud", "secrets", "describe", secret, project_flag,
                            "--format=json", allow_missing=True)
        if existing is None:
            command("gcloud", "secrets", "create", secret, project_flag,
                    "--replication-policy=automatic", "--quiet")
    for account in plan["service_accounts"]:
        email = account["email"]
        existing = document("gcloud", "iam", "service-accounts", "describe", email,
                            project_flag, "--format=json", allow_missing=True)
        if existing is None:
            command("gcloud", "iam", "service-accounts", "create", account["name"], project_flag,
                    f"--display-name={account['name']}", "--quiet")
        else:
            ensure_enabled(existing, email)
        ensure_binding(("gcloud", "iam", "service-accounts"), email,
                       account["principal"], "roles/iam.workloadIdentityUser")
        for secret in account["secrets"]:
            ensure_binding(("gcloud", "secrets"), secret,
                           f"serviceAccount:{email}", "roles/secretmanager.secretAccessor")
        set_repository_variable(account["repository"], "GCP_WORKLOAD_IDENTITY_PROVIDER", PROVIDER_RESOURCE)
        set_repository_variable(account["repository"], "GCP_SERVICE_ACCOUNT", email)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--apply", action="store_true", help="Create resources, add scoped IAM bindings, and set repository variables")
    args = parser.parse_args(argv)
    try:
        identifiers = repository_ids()
        validate_google_project()
        plan = resource_plan(identifiers)
        print(json.dumps(plan, indent=2))
        if args.apply:
            apply_plan(plan)
            print("Identity resources and repository variables configured. Populate secret versions separately.")
        else:
            print("Plan only: no resources changed. Run with --apply to configure this plan.")
    except ConfigurationError as error:
        parser.exit(1, f"{error}\n")


if __name__ == "__main__":
    main()
