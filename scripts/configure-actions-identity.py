#!/usr/bin/env python3
"""Plan GitHub OIDC identity setup; use --apply to provision it without secret values.

Requires authenticated gcloud only. Existing matching resources and IAM bindings
are retained. Conflicting trust configuration is never overwritten. Repository
names are trusted within an immutable GitHub owner ID: the same owner recreating
one of these repository names retains its trust; a different owner cannot.
"""

import argparse
import json
import re
import subprocess
import sys
import time

PROJECT = "macro-events-882dcb"
PROJECT_NUMBER = "186933910776"
OWNER_ID = "2191702"
POOL = "systems-actions"
PROVIDER = "github"
ISSUER = "https://token.actions.githubusercontent.com"
DEPLOY_WORKFLOW = "freddierice/systems-deployment/.github/workflows/deploy.yml@refs/heads/main"
LOCK_BUCKET = "freddie-systems-actions-186933910776"
RETRY_DELAYS = (2, 4, 8, 16, 30)
MISSING_ERROR = re.compile(r"\bNOT_FOUND\b|\(HTTP 404\)|\bHTTPError 404\b|\bnot found: 404\b")
PROPAGATION_ERROR = re.compile(
    r"(?:service account|(?:workload )?identity pool|principal).*?"
    r"(?:does not exist|not found|not (?:yet )?(?:active|ready))|"
    r"\b(?:SERVICE_DISABLED|UNAVAILABLE|DEADLINE_EXCEEDED|ABORTED|INTERNAL|RESOURCE_EXHAUSTED)\b|"
    r"\bHTTPError (?:408|429|500|502|503|504)\b",
    re.IGNORECASE | re.DOTALL,
)
PROVIDER_RESOURCE = (
    f"projects/{PROJECT_NUMBER}/locations/global/workloadIdentityPools/{POOL}/providers/{PROVIDER}"
)
TARGETS = {
    "health": {
        "repository": "freddierice/health",
        "workflow": "release.yml",
        "identity": "freddierice/health",
        "secrets": ("systems-actions-digitalocean",),
    },
    "trends": {
        "repository": "freddierice/trends",
        "workflow": "release.yml",
        "identity": "freddierice/trends",
        "secrets": ("systems-actions-digitalocean",),
    },
    "deploy": {
        "identity": "deploy",
        "secrets": ("systems-actions-digitalocean-deploy", "systems-actions-git-key"),
    },
}
MAPPING = {"google.subject": "assertion.sub"} | {
    f"attribute.{claim}": f"assertion.{claim}"
    for claim in (
        "repository_owner_id", "repository", "ref",
        "event_name", "workflow_ref", "runner_environment",
    )
}
MAPPING["attribute.identity"] = (
    f"('job_workflow_ref' in assertion && assertion.job_workflow_ref == '{DEPLOY_WORKFLOW}') "
    "? 'deploy' : assertion.repository"
)


class ConfigurationError(RuntimeError):
    pass


def command(*arguments, allow_missing=False, payload=None):
    """Retry bounded propagation failures; never print captured response bodies."""
    label = " ".join(arguments[:4])
    for attempt in range(len(RETRY_DELAYS) + 1):
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
            raise ConfigurationError(f"Could not run {label} ({type(error).__name__}).") from None
        if result.returncode == 0:
            return result.stdout
        missing = MISSING_ERROR.search(result.stderr)
        if allow_missing and missing:
            return None
        if (missing or PROPAGATION_ERROR.search(result.stderr)) and attempt < len(RETRY_DELAYS):
            delay = RETRY_DELAYS[attempt]
            print(f"{label}: temporary resource/API propagation failure; retrying in {delay}s.", file=sys.stderr)
            time.sleep(delay)
            continue
        # These calls manage public resource metadata and IAM only; none read
        # secret versions, keys, or tokens. Preserve actionable CLI diagnostics
        # while keeping stdout (resource response bodies) out of error output.
        diagnostic = result.stderr.strip()[-2000:] or "No CLI diagnostic was returned."
        raise ConfigurationError(
            f"{label} failed (exit {result.returncode}, attempt {attempt + 1}):\n{diagnostic}"
        )


def document(*arguments, allow_missing=False):
    output = command(*arguments, allow_missing=allow_missing)
    if output is None:
        return None
    try:
        return json.loads(output)
    except ValueError:
        raise ConfigurationError(f"{arguments[0]} returned invalid JSON.") from None


def validate_google_project():
    data = document("gcloud", "projects", "describe", PROJECT, "--format=json")
    if data.get("projectId") != PROJECT or str(data.get("projectNumber")) != PROJECT_NUMBER:
        raise ConfigurationError("Google project identity did not match the expected project.")


def resource_plan():
    clauses = []
    accounts = []
    for name, target in TARGETS.items():
        if name != "deploy":
            repository = target["repository"]
            workflow = f"{repository}/.github/workflows/{target['workflow']}@refs/heads/main"
            clauses.append(
                f"(assertion.repository == '{repository}' && assertion.workflow_ref == '{workflow}')"
            )
        account = f"systems-ci-{name}"
        accounts.append({
            "name": account,
            "email": f"{account}@{PROJECT}.iam.gserviceaccount.com",
            "identity": target["identity"],
            "principal": (
                f"principalSet://iam.googleapis.com/projects/{PROJECT_NUMBER}/locations/global/"
                f"workloadIdentityPools/{POOL}/attribute.identity/{target['identity']}"
            ),
            "secrets": list(target["secrets"]),
        })
    condition = (
        f"assertion.repository_owner_id == '{OWNER_ID}' && "
        "assertion.ref == 'refs/heads/main' && "
        "assertion.event_name == 'push' && "
        "assertion.runner_environment == 'github-hosted' && (" + " || ".join(clauses) + ") && "
        # Ordinary jobs can identify their own workflow in job_workflow_ref too.
        # The caller clauses above already pin workflow_ref to each release.yml.
        "(!('job_workflow_ref' in assertion) || assertion.job_workflow_ref == assertion.workflow_ref || "
        f"assertion.job_workflow_ref == '{DEPLOY_WORKFLOW}')"
    )
    return {
        "project": PROJECT,
        "pool": POOL,
        "provider": PROVIDER_RESOURCE,
        "attribute_mapping": MAPPING,
        "attribute_condition": condition,
        "service_accounts": accounts,
        "empty_secrets_if_absent": sorted({secret for target in TARGETS.values() for secret in target["secrets"]}),
        "lock_bucket": {
            "url": f"gs://{LOCK_BUCKET}",
            "location": "US-EAST1",
            "uniform_bucket_level_access": True,
            "public_access_prevention": "enforced",
            "member": f"serviceAccount:systems-ci-deploy@{PROJECT}.iam.gserviceaccount.com",
            "role": "roles/storage.objectUser",
        },
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


def ensure_lock_bucket(plan):
    bucket = plan["lock_bucket"]
    existing = document("gcloud", "storage", "buckets", "describe", bucket["url"],
                        f"--project={PROJECT}", "--raw", "--format=json", allow_missing=True)
    if existing is None:
        command("gcloud", "storage", "buckets", "create", bucket["url"],
                f"--project={PROJECT}", f"--location={bucket['location']}",
                "--uniform-bucket-level-access", "--public-access-prevention", "--quiet")
        return
    iam = existing.get("iamConfiguration", {})
    if (
        existing.get("name") != LOCK_BUCKET
        or str(existing.get("projectNumber")) != PROJECT_NUMBER
        or existing.get("location", "").upper() not in ("US", "US-EAST1")
        or not iam.get("uniformBucketLevelAccess", {}).get("enabled")
        or iam.get("publicAccessPrevention") != "enforced"
    ):
        raise ConfigurationError("Existing lock bucket ownership/security differs from the plan; refusing to use it.")


def apply_plan(plan):
    project_flag = f"--project={PROJECT}"
    command("gcloud", "services", "enable", "iam.googleapis.com", "iamcredentials.googleapis.com",
            "sts.googleapis.com", "secretmanager.googleapis.com", "storage.googleapis.com", project_flag, "--quiet")
    ensure_provider(plan)
    ensure_lock_bucket(plan)
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
    bucket = plan["lock_bucket"]
    ensure_binding(("gcloud", "storage", "buckets"), bucket["url"], bucket["member"], bucket["role"])


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--apply", action="store_true", help="Create Google identity resources, lock bucket, and scoped IAM bindings")
    args = parser.parse_args(argv)
    try:
        validate_google_project()
        plan = resource_plan()
        print(json.dumps(plan, indent=2))
        if args.apply:
            apply_plan(plan)
            print("Google identity resources and lock bucket configured. Populate secret versions separately.")
        else:
            print("Plan only: no resources changed. Run with --apply to configure this plan.")
    except ConfigurationError as error:
        parser.exit(1, f"{error}\n")


if __name__ == "__main__":
    main()
