"""Check OIDC trust and narrow IAM grants without cloud or GitHub mutations."""

import contextlib
import importlib.util
import io
import subprocess
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

SPEC = importlib.util.spec_from_file_location(
    "actions_identity", Path(__file__).resolve().parents[1] / "scripts/configure-actions-identity.py"
)
identity = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(identity)
IDS = {"health": "101", "trends": "102", "deploy": "1360448012"}


def accepts(condition, claims):
    # Generated CEL uses only equality and boolean operators. Evaluate that subset
    # to test accepted/rejected claims rather than just matching condition text.
    expression = condition.replace("&&", "and").replace("||", "or")
    return eval(expression, {"__builtins__": {}}, {"assertion": SimpleNamespace(**claims)})


class TrustTests(unittest.TestCase):
    def test_only_expected_hosted_main_workflows_and_events_are_accepted(self):
        condition = identity.resource_plan(IDS)["attribute_condition"]
        for name, target in identity.TARGETS.items():
            claims = {
                "repository_owner_id": "2191702",
                "repository_id": IDS[name],
                "repository": target["repository"],
                "ref": "refs/heads/main",
                "event_name": target["event"],
                "workflow_ref": f"{target['repository']}/.github/workflows/{target['workflow']}@refs/heads/main",
                "runner_environment": "github-hosted",
            }
            with self.subTest(name=name):
                self.assertTrue(accepts(condition, claims))
            denied = {
                "repository_owner_id": "9999",
                "repository_id": "8888",
                "repository": "other/health",
                "ref": "refs/heads/feature",
                "event_name": "pull_request_target",
                "workflow_ref": f"{target['repository']}/.github/workflows/untrusted.yml@refs/heads/main",
                "runner_environment": "self-hosted",
            }
            for field, value in denied.items():
                with self.subTest(name=name, field=field):
                    self.assertFalse(accepts(condition, claims | {field: value}))
            wrong_event = "push" if name == "deploy" else "repository_dispatch"
            self.assertFalse(accepts(condition, claims | {"event_name": wrong_event}))
            self.assertFalse(accepts(condition, claims | {"workflow_ref": claims["workflow_ref"].replace("@refs/heads/main", "@refs/heads/feature")}))

    def test_secret_access_and_impersonation_are_scoped_per_identity(self):
        accounts = {account["name"]: account for account in identity.resource_plan(IDS)["service_accounts"]}
        for app in ("health", "trends"):
            account = accounts[f"systems-ci-{app}"]
            self.assertEqual(set(account["secrets"]), {"systems-actions-digitalocean", "systems-actions-github-dispatch"})
            self.assertTrue(account["principal"].endswith(f"/attribute.repository_id/{IDS[app]}"))
        self.assertEqual(set(accounts["systems-ci-deploy"]["secrets"]), {
            "systems-actions-digitalocean-deploy", "systems-actions-github-dispatch",
        })
        self.assertEqual(identity.MAPPING["attribute.repository_id"], "assertion.repository_id")

    def test_repository_lookup_rejects_owner_or_identity_substitution(self):
        responses = [
            {"full_name": target["repository"], "id": int(IDS[name]), "owner": {"id": 2191702}, "default_branch": "main"}
            for name, target in identity.TARGETS.items()
        ]
        with patch.object(identity, "command"), patch.object(identity, "document", side_effect=responses):
            self.assertEqual(identity.repository_ids(), IDS)
        for changed in ({"owner": {"id": 42}}, {"id": True}, {"default_branch": "feature"}):
            with (
                patch.object(identity, "command"),
                patch.object(identity, "document", return_value=responses[0] | changed),
                self.assertRaises(identity.ConfigurationError),
            ):
                identity.repository_ids()


class ProvisioningTests(unittest.TestCase):
    def test_default_mode_never_applies(self):
        with patch.object(identity, "repository_ids", return_value=IDS), patch.object(identity, "validate_google_project"), patch.object(identity, "apply_plan") as apply, contextlib.redirect_stdout(io.StringIO()) as output:
            identity.main([])
        apply.assert_not_called()
        self.assertIn("Plan only", output.getvalue())

    def test_permission_failure_is_not_mistaken_for_an_absent_resource(self):
        failure = subprocess.CompletedProcess(["gcloud"], 1, "", "PERMISSION_DENIED: access blocked")
        with (
            patch.object(identity.subprocess, "run", return_value=failure),
            self.assertRaises(identity.ConfigurationError),
        ):
            identity.command("gcloud", "secrets", "describe", "example", allow_missing=True)
        failure.stderr = "NOT_FOUND: resource does not exist"
        with patch.object(identity.subprocess, "run", return_value=failure):
            self.assertIsNone(identity.command("gcloud", "secrets", "describe", "example", allow_missing=True))

    def test_existing_conflicting_provider_is_not_overwritten(self):
        plan = identity.resource_plan(IDS)
        existing = {"name": identity.PROVIDER_RESOURCE, "oidc": {"issuerUri": identity.ISSUER},
                    "attributeMapping": identity.MAPPING, "attributeCondition": "true"}
        with (
            patch.object(identity, "document", side_effect=[{"state": "ACTIVE"}, [existing]]),
            patch.object(identity, "command") as mutate,
            self.assertRaisesRegex(identity.ConfigurationError, "refusing to overwrite"),
        ):
            identity.ensure_provider(plan)
        mutate.assert_not_called()

    def test_matching_provider_and_unconditional_binding_are_noops(self):
        plan = identity.resource_plan(IDS)
        existing = {"name": identity.PROVIDER_RESOURCE, "oidc": {"issuerUri": identity.ISSUER},
                    "attributeMapping": identity.MAPPING, "attributeCondition": plan["attribute_condition"]}
        with patch.object(identity, "document", side_effect=[{"state": "ACTIVE"}, [existing]]), patch.object(identity, "command") as mutate:
            identity.ensure_provider(plan)
        mutate.assert_not_called()
        policy = {"bindings": [{"role": "roles/secretmanager.secretAccessor", "members": ["serviceAccount:example"]}]}
        with patch.object(identity, "document", return_value=policy), patch.object(identity, "command") as mutate:
            identity.ensure_binding(("gcloud", "secrets"), "example", "serviceAccount:example", "roles/secretmanager.secretAccessor")
        mutate.assert_not_called()

    def test_apply_only_adds_scoped_bindings_and_never_secret_versions_or_keys(self):
        with patch.object(identity, "ensure_provider"), patch.object(identity, "document", return_value=None), patch.object(identity, "ensure_binding") as bind, patch.object(identity, "set_repository_variable") as variable, patch.object(identity, "command") as commands:
            identity.apply_plan(identity.resource_plan(IDS))
        self.assertEqual(bind.call_count, 9)
        self.assertEqual(variable.call_count, 6)
        for call in bind.call_args_list:
            prefix, resource, member, role = call.args
            if role == "roles/iam.workloadIdentityUser":
                self.assertEqual(prefix, ("gcloud", "iam", "service-accounts"))
                self.assertTrue(resource.startswith("systems-ci-"))
                self.assertIn("/attribute.repository_id/", member)
            else:
                self.assertEqual(prefix, ("gcloud", "secrets"))
                self.assertEqual(role, "roles/secretmanager.secretAccessor")
        for call in commands.call_args_list:
            self.assertNotIn("versions", call.args)
            self.assertNotIn("keys", call.args)
            self.assertNotIn("add-iam-policy-binding", call.args)

    def test_binding_command_places_resource_after_operation(self):
        with patch.object(identity, "document", return_value={}), patch.object(identity, "command") as mutate:
            identity.ensure_binding(("gcloud", "secrets"), "example", "serviceAccount:example", "roles/secretmanager.secretAccessor")
        self.assertEqual(mutate.call_args.args[:4], ("gcloud", "secrets", "add-iam-policy-binding", "example"))

    def test_repository_variable_update_is_idempotent_and_structured(self):
        with patch.object(identity, "document", return_value={"value": "same"}), patch.object(identity, "command") as mutate:
            identity.set_repository_variable("freddierice/health", "EXAMPLE", "same")
        mutate.assert_not_called()
        with patch.object(identity, "document", return_value=None), patch.object(identity, "command") as mutate:
            identity.set_repository_variable("freddierice/health", "EXAMPLE", "new")
        self.assertIn("POST", mutate.call_args.args)
        self.assertEqual(mutate.call_args.kwargs["payload"], {"name": "EXAMPLE", "value": "new"})


if __name__ == "__main__":
    unittest.main()
