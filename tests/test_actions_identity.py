"""Check reusable-workflow trust and scoped Google IAM without cloud mutations."""
import contextlib
import copy
import importlib.util
import io
import subprocess
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

SPEC = importlib.util.spec_from_file_location("actions_identity", Path(__file__).resolve().parents[1] / "scripts/configure-actions-identity.py")
identity = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(identity)


class Claims(SimpleNamespace):
    def __contains__(self, key):
        return hasattr(self, key)


def evaluate(expression, claims):
    # Evaluate generated CEL equality/boolean/optional-claim/conditional behavior.
    if " ? " in expression:
        condition, choices = expression.split(" ? ", 1)
        yes, no = choices.split(" : ", 1)
        return evaluate(yes if evaluate(condition, claims) else no, claims)
    expression = expression.replace("&&", "and").replace("||", "or").replace("!(", "not (")
    return eval(expression, {"__builtins__": {}}, {"assertion": Claims(**claims)})


def caller(app="health", **overrides):
    repository = f"freddierice/{app}"
    return {"repository_owner_id": identity.OWNER_ID, "repository": repository,
            "ref": "refs/heads/main", "event_name": "push", "runner_environment": "github-hosted",
            "workflow_ref": f"{repository}/.github/workflows/release.yml@refs/heads/main"} | overrides


class TrustTests(unittest.TestCase):
    def test_only_expected_hosted_main_push_callers_are_accepted(self):
        condition = identity.resource_plan()["attribute_condition"]
        for app in ("health", "trends", "time"):
            claims = caller(app)
            self.assertTrue(evaluate(condition, claims))
            self.assertTrue(evaluate(condition, claims | {"job_workflow_ref": identity.DEPLOY_WORKFLOW}))
            denied = {"repository_owner_id": "9999", "repository": "someone-else/health",
                      "ref": "refs/heads/feature", "event_name": "pull_request_target",
                      "workflow_ref": f"freddierice/{app}/.github/workflows/other.yml@refs/heads/main",
                      "runner_environment": "self-hosted"}
            for field, value in denied.items():
                for reusable in ({}, {"job_workflow_ref": identity.DEPLOY_WORKFLOW}):
                    with self.subTest(app=app, field=field, reusable=bool(reusable)):
                        self.assertFalse(evaluate(condition, claims | reusable | {field: value}))
            for event in ("pull_request", "workflow_dispatch", "repository_dispatch"):
                self.assertFalse(evaluate(condition, claims | {"event_name": event}))
        self.assertFalse(evaluate(condition, caller("systems-deployment")))

    def test_only_exact_reusable_workflow_gets_deploy_identity(self):
        condition, mapping = identity.resource_plan()["attribute_condition"], identity.MAPPING["attribute.identity"]
        for app in ("health", "trends", "time"):
            publisher = caller(app)
            self.assertEqual(evaluate(mapping, publisher), f"freddierice/{app}")
            own_job = publisher | {"job_workflow_ref": publisher["workflow_ref"]}
            self.assertTrue(evaluate(condition, own_job))
            self.assertEqual(evaluate(mapping, own_job), f"freddierice/{app}")
            deployment = publisher | {"job_workflow_ref": identity.DEPLOY_WORKFLOW}
            self.assertTrue(evaluate(condition, deployment))
            self.assertEqual(evaluate(mapping, deployment), "deploy")
            for wrong in (identity.DEPLOY_WORKFLOW.replace("deploy.yml", "other.yml"),
                          identity.DEPLOY_WORKFLOW.replace("@refs/heads/main", "@refs/heads/feature"),
                          identity.DEPLOY_WORKFLOW.replace("freddierice/", "different-owner/"),
                          caller("trends" if app == "health" else "health")["workflow_ref"], ""):
                claims = publisher | {"job_workflow_ref": wrong}
                self.assertFalse(evaluate(condition, claims))
                self.assertNotEqual(evaluate(mapping, claims), "deploy")

    def test_secret_and_bucket_access_are_separate(self):
        plan = identity.resource_plan()
        accounts = {account["name"]: account for account in plan["service_accounts"]}
        for name, app in (("health", "health"), ("trends", "trends"), ("daily-report", "time")):
            account = accounts[f"systems-ci-{name}"]
            self.assertEqual(account["secrets"], ["systems-actions-digitalocean"])
            self.assertEqual(account["identity"], f"freddierice/{app}")
            self.assertTrue(account["principal"].endswith(f"/attribute.identity/freddierice/{app}"))
        self.assertEqual(set(plan["empty_secrets_if_absent"]), {
            "systems-actions-digitalocean", "systems-actions-digitalocean-deploy", "systems-actions-git-key",
        })
        deployment = accounts["systems-ci-deploy"]
        self.assertEqual(set(deployment["secrets"]), {"systems-actions-digitalocean-deploy", "systems-actions-git-key"})
        self.assertTrue(deployment["principal"].endswith("/attribute.identity/deploy"))
        self.assertEqual(plan["lock_bucket"]["member"], f"serviceAccount:{deployment['email']}")
        self.assertEqual(plan["lock_bucket"]["role"], "roles/storage.objectUser")
        self.assertEqual(identity.MAPPING["attribute.repository"], "assertion.repository")
        self.assertNotIn("repository_variables", plan)

    def test_name_trust_stays_inside_immutable_owner(self):
        condition = identity.resource_plan()["attribute_condition"]
        self.assertTrue(evaluate(condition, caller(repository_id="new-id-under-same-owner")))
        self.assertFalse(evaluate(condition, caller(repository_owner_id="new-owner-id")))

    def test_recognized_previous_trust_accepts_existing_callers_but_not_daily_report(self):
        previous = identity.resource_plan()["recognized_previous_attribute_condition"]
        for app in ("health", "trends"):
            self.assertTrue(evaluate(previous, caller(app)))
            self.assertTrue(evaluate(previous, caller(app, job_workflow_ref=identity.DEPLOY_WORKFLOW)))
        self.assertFalse(evaluate(previous, caller("time")))
        self.assertFalse(evaluate(previous, caller("time", job_workflow_ref=identity.DEPLOY_WORKFLOW)))


class ProvisioningTests(unittest.TestCase):
    def test_new_pool_and_service_account_propagation_are_retried(self):
        failures = [
            "NOT_FOUND: Workload identity pool was not found.",
            "INVALID_ARGUMENT: Service account systems-ci-health@example.iam.gserviceaccount.com does not exist.",
        ]
        for diagnostic in failures:
            with self.subTest(diagnostic=diagnostic), \
                 patch.object(identity.subprocess, "run", side_effect=[subprocess.CompletedProcess([], 1, "", diagnostic), subprocess.CompletedProcess([], 0, "configured", "")]) as execute, \
                 patch.object(identity.time, "sleep") as sleep, contextlib.redirect_stderr(io.StringIO()):
                self.assertEqual(identity.command("gcloud", "iam", "service-accounts", "add-iam-policy-binding", "example"), "configured")
                self.assertEqual(execute.call_count, 2)
                sleep.assert_called_once_with(2)

    def test_transient_retry_is_bounded_and_preserves_final_diagnostic(self):
        failure = subprocess.CompletedProcess([], 1, "private response body", "UNAVAILABLE: backend is not ready")
        with patch.object(identity.subprocess, "run", return_value=failure) as execute, \
             patch.object(identity.time, "sleep") as sleep, contextlib.redirect_stderr(io.StringIO()), \
             self.assertRaises(identity.ConfigurationError) as caught:
            identity.command("gcloud", "iam", "workload-identity-pools", "create", "example")
        self.assertEqual(execute.call_count, len(identity.RETRY_DELAYS) + 1)
        self.assertEqual([call.args[0] for call in sleep.call_args_list], list(identity.RETRY_DELAYS))
        self.assertIn("UNAVAILABLE: backend is not ready", str(caught.exception))
        self.assertNotIn("private response body", str(caught.exception))

    def test_permission_and_bad_configuration_fail_immediately_with_diagnosis(self):
        for diagnostic in ("PERMISSION_DENIED: missing iam.serviceAccounts.setIamPolicy", "INVALID_ARGUMENT: invalid attribute mapping"):
            failure = subprocess.CompletedProcess([], 1, "", diagnostic)
            with self.subTest(diagnostic=diagnostic), patch.object(identity.subprocess, "run", return_value=failure) as execute, \
                 patch.object(identity.time, "sleep") as sleep, self.assertRaises(identity.ConfigurationError) as caught:
                identity.command("gcloud", "iam", "service-accounts", "add-iam-policy-binding", "example")
            execute.assert_called_once()
            sleep.assert_not_called()
            self.assertIn(diagnostic, str(caught.exception))

    def test_default_plan_needs_only_gcloud_and_never_applies(self):
        project = {"projectId": identity.PROJECT, "projectNumber": identity.PROJECT_NUMBER}
        with patch.object(identity, "document", return_value=project) as read, patch.object(identity, "apply_plan") as apply, contextlib.redirect_stdout(io.StringIO()) as output:
            identity.main([])
        read.assert_called_once_with("gcloud", "projects", "describe", identity.PROJECT, "--format=json")
        apply.assert_not_called()
        self.assertIn("Plan only", output.getvalue())

    def test_permission_failure_is_not_an_absent_resource(self):
        failure = subprocess.CompletedProcess(["gcloud"], 1, "", "PERMISSION_DENIED: access blocked")
        with patch.object(identity.subprocess, "run", return_value=failure), self.assertRaises(identity.ConfigurationError):
            identity.command("gcloud", "secrets", "describe", "example", allow_missing=True)
        for message in ("NOT_FOUND: resource does not exist", "HTTPError 404: The specified bucket does not exist.", "gs://example not found: 404."):
            failure.stderr = message
            with self.subTest(message=message), patch.object(identity.subprocess, "run", return_value=failure):
                self.assertIsNone(identity.command("gcloud", "storage", "buckets", "describe", "gs://example", allow_missing=True))

    def test_conflicting_or_additional_provider_is_not_used(self):
        plan = identity.resource_plan()
        existing = {"name": identity.PROVIDER_RESOURCE, "oidc": {"issuerUri": identity.ISSUER},
                    "attributeMapping": identity.MAPPING, "attributeCondition": "true"}
        for providers in ([existing], [existing | {"name": identity.PROVIDER_RESOURCE + "-other"}]):
            with patch.object(identity, "document", side_effect=[{"state": "ACTIVE"}, providers]), patch.object(identity, "command") as mutate, self.assertRaises(identity.ConfigurationError):
                identity.ensure_provider(plan)
            mutate.assert_not_called()

    def test_matching_provider_and_binding_are_noops(self):
        plan = identity.resource_plan()
        existing = {"name": identity.PROVIDER_RESOURCE, "oidc": {"issuerUri": identity.ISSUER},
                    "attributeMapping": identity.MAPPING, "attributeCondition": plan["attribute_condition"]}
        with patch.object(identity, "document", side_effect=[{"state": "ACTIVE"}, [existing]]), patch.object(identity, "command") as mutate:
            identity.ensure_provider(plan)
        mutate.assert_not_called()
        policy = {"bindings": [{"role": "roles/secretmanager.secretAccessor", "members": ["serviceAccount:example"]}]}
        with patch.object(identity, "document", return_value=policy), patch.object(identity, "command") as mutate:
            identity.ensure_binding(("gcloud", "secrets"), "example", "serviceAccount:example", "roles/secretmanager.secretAccessor")
        mutate.assert_not_called()

    def test_exact_previous_condition_is_upgraded_once_then_idempotent(self):
        plan = identity.resource_plan()
        existing = {"name": identity.PROVIDER_RESOURCE, "oidc": {"issuerUri": identity.ISSUER},
                    "attributeMapping": identity.MAPPING,
                    "attributeCondition": identity.PRE_DAILY_REPORT_CONDITION}

        def apply_update(*arguments):
            self.assertEqual(arguments[:6], (
                "gcloud", "iam", "workload-identity-pools", "providers", "update-oidc", identity.PROVIDER,
            ))
            self.assertIn(f"--workload-identity-pool={identity.POOL}", arguments)
            self.assertIn(f"--project={identity.PROJECT}", arguments)
            self.assertFalse(any(arg.startswith(("--issuer-uri", "--attribute-mapping", "--allowed-audiences"))
                                 for arg in arguments))
            existing["attributeCondition"] = next(
                arg.split("=", 1)[1] for arg in arguments if arg.startswith("--attribute-condition=")
            )

        with patch.object(identity, "document", side_effect=[{"state": "ACTIVE"}, [existing]] * 2), \
             patch.object(identity, "command", side_effect=apply_update) as mutate:
            identity.ensure_provider(plan)
            self.assertEqual(existing["attributeCondition"], plan["attribute_condition"])
            identity.ensure_provider(plan)
        mutate.assert_called_once()

    def test_prior_condition_does_not_allow_other_trust_changes(self):
        plan = identity.resource_plan()
        existing = {"name": identity.PROVIDER_RESOURCE, "oidc": {"issuerUri": identity.ISSUER},
                    "attributeMapping": identity.MAPPING,
                    "attributeCondition": identity.PRE_DAILY_REPORT_CONDITION}
        variants = [
            existing | {"oidc": {"issuerUri": "https://different-issuer.example"}},
            existing | {"oidc": {"issuerUri": identity.ISSUER, "allowedAudiences": ["unexpected"]}},
            existing | {"attributeMapping": identity.MAPPING | {"attribute.identity": "'deploy'"}},
            existing | {"disabled": True},
            existing | {"state": "DELETED"},
        ]
        for previous in ("true", identity.PRE_DAILY_REPORT_CONDITION + " || true",
                         identity.PRE_DAILY_REPORT_CONDITION.replace("2191702", "9999"),
                         identity.PRE_DAILY_REPORT_CONDITION.replace("github-hosted", "self-hosted"),
                         identity.PRE_DAILY_REPORT_CONDITION.replace("refs/heads/main", "refs/heads/feature")):
            variants.append(existing | {"attributeCondition": previous})
        for provider in variants:
            with self.subTest(provider=provider), \
                 patch.object(identity, "document", side_effect=[{"state": "ACTIVE"}, [provider]]), \
                 patch.object(identity, "command") as mutate, self.assertRaises(identity.ConfigurationError):
                identity.ensure_provider(plan)
            mutate.assert_not_called()
        with patch.object(identity, "document", side_effect=[{"state": "ACTIVE"}, [existing]]), \
             patch.object(identity, "command") as mutate, self.assertRaises(identity.ConfigurationError):
            identity.ensure_provider(plan | {"attribute_condition": "true"})
        mutate.assert_not_called()

    def test_apply_only_uses_scoped_google_grants_not_secret_values_or_keys(self):
        with patch.object(identity, "ensure_provider"), patch.object(identity, "ensure_lock_bucket"), patch.object(identity, "document", return_value=None), patch.object(identity, "ensure_binding") as bind, patch.object(identity, "command") as commands:
            identity.apply_plan(identity.resource_plan())
        self.assertEqual(bind.call_count, 10)
        publisher = f"serviceAccount:systems-ci-daily-report@{identity.PROJECT}.iam.gserviceaccount.com"
        publisher_grants = [call.args for call in bind.call_args_list if call.args[2] == publisher]
        self.assertEqual(publisher_grants, [
            (("gcloud", "secrets"), "systems-actions-digitalocean", publisher, "roles/secretmanager.secretAccessor"),
        ])
        for call in bind.call_args_list:
            prefix, resource, member, role = call.args
            if role == "roles/iam.workloadIdentityUser":
                self.assertEqual(prefix, ("gcloud", "iam", "service-accounts"))
                self.assertIn("/attribute.identity/", member)
            elif role == "roles/storage.objectUser":
                self.assertEqual(prefix, ("gcloud", "storage", "buckets"))
                self.assertEqual(resource, f"gs://{identity.LOCK_BUCKET}")
                self.assertIn("systems-ci-deploy@", member)
            else:
                self.assertEqual(prefix, ("gcloud", "secrets"))
                self.assertEqual(role, "roles/secretmanager.secretAccessor")
        for call in commands.call_args_list:
            self.assertEqual(call.args[0], "gcloud")
            self.assertNotIn("versions", call.args)
            self.assertNotIn("keys", call.args)
            self.assertNotIn("add-iam-policy-binding", call.args)

    def test_lock_bucket_security_and_project_are_checked(self):
        valid = {"name": identity.LOCK_BUCKET, "projectNumber": identity.PROJECT_NUMBER, "location": "US-EAST1",
                 "iamConfiguration": {"uniformBucketLevelAccess": {"enabled": True}, "publicAccessPrevention": "enforced"}}
        with patch.object(identity, "document", return_value=valid), patch.object(identity, "command") as mutate:
            identity.ensure_lock_bucket(identity.resource_plan())
        mutate.assert_not_called()
        bad = copy.deepcopy(valid)
        bad["iamConfiguration"]["publicAccessPrevention"] = "inherited"
        for data in (bad, valid | {"projectNumber": "different-project"}):
            with patch.object(identity, "document", return_value=data), self.assertRaises(identity.ConfigurationError):
                identity.ensure_lock_bucket(identity.resource_plan())

    def test_new_lock_bucket_blocks_public_and_per_object_access(self):
        with patch.object(identity, "document", return_value=None), patch.object(identity, "command") as mutate:
            identity.ensure_lock_bucket(identity.resource_plan())
        arguments = mutate.call_args.args
        self.assertEqual(arguments[:5], ("gcloud", "storage", "buckets", "create", f"gs://{identity.LOCK_BUCKET}"))
        self.assertIn("--uniform-bucket-level-access", arguments)
        self.assertIn("--public-access-prevention", arguments)

    def test_binding_command_places_resource_after_operation(self):
        with patch.object(identity, "document", return_value={}), patch.object(identity, "command") as mutate:
            identity.ensure_binding(("gcloud", "secrets"), "example", "serviceAccount:example", "roles/secretmanager.secretAccessor")
        self.assertEqual(mutate.call_args.args[:4], ("gcloud", "secrets", "add-iam-policy-binding", "example"))


if __name__ == "__main__":
    unittest.main()
