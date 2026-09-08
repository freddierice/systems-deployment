"""Daily image releases must neither run a report nor alter its schedule/state."""
import copy
import importlib.util
import json
from pathlib import Path
import subprocess
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

import yaml


ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location("daily_release", ROOT / "scripts/deploy-app.py")
release = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(release)
VALUES = (ROOT / release.VALUES).read_text()
SHA = "a" * 40
DIGEST = "sha256:" + "b" * 64
IMAGE = f"registry.digitalocean.com/freddierice-systems/daily-report@{DIGEST}"


def git(repo, *arguments):
    return subprocess.check_output(["git", "-C", str(repo), *arguments], text=True, stderr=subprocess.DEVNULL).strip()


def commit(repo, message):
    git(repo, "add", ".")
    git(repo, "-c", "user.name=Test", "-c", "user.email=test@example.com", "commit", "-m", message)
    return git(repo, "rev-parse", "HEAD")


def with_suspension(manifest, suspended):
    resources = list(yaml.safe_load_all(manifest))
    cron = next(r for r in resources if r["kind"] == "CronJob" and r["metadata"]["name"] == "daily-report")
    cron["spec"]["suspend"] = suspended
    return yaml.safe_dump_all(resources)


class DailyReportReleaseTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.before = subprocess.check_output([
            "helm", "template", "systems", str(ROOT / release.CHART), "--namespace", "systems",
            "--values", str(ROOT / release.VALUES),
        ], text=True)
        cls.old_image = yaml.safe_load(VALUES)["dailyReport"]["image"]
        cls.after = cls.before.replace(cls.old_image, IMAGE)

    def test_repository_mapping_and_values_preserve_apps_schedule_and_suspension(self):
        release.validate_inputs("daily-report", SHA, DIGEST, "123")
        self.assertEqual(release.REPOSITORIES["daily-report"], "https://github.com/freddierice/time.git")
        updated, previous = release.update_values(VALUES, "daily-report", IMAGE, SHA)
        expected = yaml.safe_load(VALUES)
        self.assertEqual(previous, expected["dailyReport"])
        expected["dailyReport"].update(image=IMAGE, sourceRevision=SHA)
        self.assertEqual(yaml.safe_load(updated), expected)
        self.assertEqual(sum(a != b for a, b in zip(VALUES.splitlines(), updated.splitlines())), 2)

    def test_sqlite_schema_file_changes_require_operator_verification(self):
        with tempfile.TemporaryDirectory() as temporary:
            repo = Path(temporary)
            git(repo, "init", "-b", "main")
            (repo / "db.py").write_text("SCHEMA = 'CREATE TABLE example(id INTEGER)'\n")
            old = commit(repo, "Initial SQLite schema")
            (repo / "README.md").write_text("Documentation\n")
            current = commit(repo, "Documentation update")
            release.verify_schema("daily-report", old, current, repo)
            (repo / "db.py").write_text("SCHEMA = 'CREATE TABLE example(id INTEGER, name TEXT)'\n")
            changed = commit(repo, "Schema update")
            with self.assertRaisesRegex(release.ReleaseError, "Migration files changed"):
                release.verify_schema("daily-report", old, changed, repo)
            release.verify_schema("daily-report", old, changed, repo, schema_verified=True)
            with self.assertRaisesRegex(release.ReleaseError, "baseline"):
                release.verify_schema("daily-report", None, changed, repo, schema_verified=True)

    def test_database_removal_and_database_free_releases_need_no_migration(self):
        with tempfile.TemporaryDirectory() as temporary:
            repo = Path(temporary)
            git(repo, "init", "-b", "main")
            (repo / "db.py").write_text("SCHEMA = 'CREATE TABLE example(id INTEGER)'\n")
            old = commit(repo, "Initial SQLite schema")
            (repo / "db.py").unlink()
            (repo / "report.py").write_text("# Report reads Health directly.\n")
            removed = commit(repo, "Remove the local database")
            release.verify_schema("daily-report", old, removed, repo)
            (repo / "README.md").write_text("Database-free report\n")
            current = commit(repo, "Documentation update")
            release.verify_schema("daily-report", removed, current, repo)
            with self.assertRaisesRegex(release.ReleaseError, "baseline"):
                release.verify_schema("daily-report", None, current, repo)
            with self.assertRaises(release.ReleaseError):
                release.verify_schema("daily-report", removed, "f" * 40, repo)
            (repo / "db.py").write_text("SCHEMA = 'CREATE TABLE example(id INTEGER)'\n")
            restored = commit(repo, "Restore the local database")
            with self.assertRaisesRegex(release.ReleaseError, "Migration files changed"):
                release.verify_schema("daily-report", current, restored, repo)

    def test_image_guard_allows_only_the_cronjob_image(self):
        for suspended in (False, True):
            before = with_suspension(self.before, suspended)
            after = with_suspension(self.after, suspended)
            self.assertFalse(release.image_only(before, after, "daily-report", self.old_image, IMAGE))
            self.assertTrue(release.image_only(after, after, "daily-report", self.old_image, IMAGE))
            for field, value in (("schedule", "0 7 * * *"), ("suspend", not suspended), ("concurrencyPolicy", "Allow")):
                changed = list(yaml.safe_load_all(after))
                cron = next(r for r in changed if r["kind"] == "CronJob")
                cron["spec"][field] = value
                with self.subTest(suspended=suspended, field=field), self.assertRaisesRegex(release.ReleaseError, "more than"):
                    release.image_only(before, yaml.safe_dump_all(changed), "daily-report", self.old_image, IMAGE)
        changed = list(yaml.safe_load_all(self.after))
        health = next(r for r in changed if r["kind"] == "Deployment" and r["metadata"]["name"] == "health")
        health["spec"]["template"]["spec"]["containers"][0]["image"] = IMAGE
        with self.assertRaises(release.ReleaseError):
            release.image_only(self.before, yaml.safe_dump_all(changed), "daily-report", self.old_image, IMAGE)

    def test_viewer_image_guard_rejects_partial_updates_and_configuration_changes(self):
        for kind, name in (("containers", "time"), ("initContainers", "cache-existing")):
            changed = list(yaml.safe_load_all(self.after))
            web = next(r for r in changed if r["kind"] == "Deployment" and r["metadata"]["name"] == "time")
            container = next(c for c in web["spec"]["template"]["spec"][kind] if c["name"] == name)
            container["image"] = self.old_image
            with self.assertRaisesRegex(release.ReleaseError, "more than"):
                release.image_only(self.before, yaml.safe_dump_all(changed), "daily-report", self.old_image, IMAGE)
            container["image"] = IMAGE
            container["command"] = ["python", "main.py", "generate"]
            with self.assertRaisesRegex(release.ReleaseError, "more than"):
                release.image_only(self.before, yaml.safe_dump_all(changed), "daily-report", self.old_image, IMAGE)

    def test_live_schedule_drift_does_not_get_overwritten(self):
        for suspended in (False, True):
            after = with_suspension(self.after, suspended)
            cron = release.resources(with_suspension(self.before, suspended))["batch/v1", "CronJob", "systems", "daily-report"]
            with patch.object(release, "output", return_value=json.dumps(cron)):
                release.verify_report_schedule(after)
            for field, value in (("suspend", not suspended), ("schedule", "0 7 * * *"), ("timeZone", "UTC")):
                live = copy.deepcopy(cron)
                live["spec"][field] = value
                with self.subTest(suspended=suspended, field=field), patch.object(release, "output", return_value=json.dumps(live)):
                    with self.assertRaisesRegex(release.ReleaseError, "scheduling differs"):
                        release.verify_report_schedule(after)

    def exercise_deploy(self, *, dry_run=False, failure=None, stale=False, advanced_during_smoke=False, suspended=None):
        initial_values, before, after = VALUES, self.before, self.after
        if suspended is not None:
            parsed = yaml.safe_load(VALUES)
            parsed["dailyReport"]["suspend"] = suspended
            initial_values = yaml.safe_dump(parsed)
            before, after = with_suspension(self.before, suspended), with_suspension(self.after, suspended)
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            values = root / release.VALUES
            values.parent.mkdir(parents=True)
            values.write_text(initial_values)
            args = SimpleNamespace(app="daily-report", source_sha=SHA, image_digest=DIGEST,
                                   run_id="123", dry_run=dry_run, schema_verified=False)
            actions = []

            def fake_output(*arguments, **kwargs):
                if arguments[:2] == ("helm", "template"):
                    return after
                if arguments[0] == "helm" and "status" in arguments:
                    return json.dumps({"version": 8, "info": {"status": "deployed"}})
                if "manifest" in arguments:
                    return before
                if "rev-parse" in arguments:
                    return "c" * 40
                return ""

            def fake_run(*arguments, **kwargs):
                actions.append(arguments)
                return SimpleNamespace(returncode=0)

            def fake_smoke(manifest):
                actions.append(("isolated-smoke",))
                self.assertEqual(manifest, after)
                if failure == "smoke":
                    raise release.ReleaseError("Smoke failed")

            main_revisions = ["f" * 40] if stale else [SHA, SHA, "f" * 40 if advanced_during_smoke else SHA]
            with patch.object(release, "ROOT", root), patch.object(release, "current_main", side_effect=main_revisions), \
                 patch.object(release, "verify_digest", side_effect=release.ReleaseError("Wrong digest") if failure == "digest" else None) as digest, \
                 patch.object(release, "verify_schema") as schema, patch.object(release, "verify_replicas"), \
                 patch.object(release, "verify_report_schedule") as schedule, patch.object(release, "output", side_effect=fake_output), \
                 patch.object(release, "run", side_effect=fake_run), patch.object(release, "report_smoke", side_effect=fake_smoke) as smoke, \
                 patch.object(release, "smoke") as web_smoke, patch.object(release, "record_release") as record:
                if failure:
                    with self.assertRaises(release.ReleaseError):
                        release.deploy(args)
                else:
                    release.deploy(args)
                if not (dry_run or failure or stale or advanced_during_smoke):
                    web_smoke.assert_called_once_with("time")
                else:
                    web_smoke.assert_not_called()
                upgrades = [action for action in actions if "upgrade" in action]
                self.assertFalse(any("rollback" in action for action in actions))
                if dry_run or failure or stale or advanced_during_smoke:
                    self.assertEqual(values.read_text(), initial_values)
                    record.assert_not_called()
                    self.assertEqual(upgrades, [])
                else:
                    self.assertEqual(yaml.safe_load(values.read_text())["dailyReport"]["image"], IMAGE)
                    self.assertEqual(yaml.safe_load(values.read_text())["dailyReport"]["suspend"],
                                     yaml.safe_load(initial_values)["dailyReport"]["suspend"])
                    record.assert_called_once_with("daily-report", SHA, "123")
                    self.assertLess(actions.index(("isolated-smoke",)), actions.index(upgrades[0]))
                    self.assertEqual(schedule.call_count, 2)
                if stale:
                    digest.assert_not_called()
                else:
                    digest.assert_called_once_with("daily-report", SHA, DIGEST)
                if dry_run or stale or failure == "digest":
                    smoke.assert_not_called()
                if not stale and failure != "digest":
                    self.assertFalse(schema.call_args.kwargs["schema_verified"])

    def test_dry_run_never_launches_smoke_or_changes_cluster(self):
        self.exercise_deploy(dry_run=True)

    def test_smoke_failure_never_updates_the_cronjob(self):
        self.exercise_deploy(failure="smoke")

    def test_daily_report_keeps_source_main_and_digest_guards(self):
        self.exercise_deploy(stale=True)
        self.exercise_deploy(failure="digest")
        self.exercise_deploy(advanced_during_smoke=True)

    def test_success_smokes_before_only_future_jobs_receive_image(self):
        for suspended in (False, True):
            with self.subTest(suspended=suspended):
                self.exercise_deploy(suspended=suspended)

    def exercise_smoke(self, phase="Succeeded", *, timed_out=False, creation_failed=False, deletion_failed=False):
        manifests = []
        actions = []

        def fake_run(*arguments, **kwargs):
            actions.append((arguments, kwargs))
            if "--filename" in arguments:
                path = Path(arguments[arguments.index("--filename") + 1])
                manifests.append(list(yaml.safe_load_all(path.read_text())))
            if creation_failed and "apply" in arguments:
                raise release.ReleaseError("Create outcome unknown")
            if deletion_failed and "delete" in arguments and "pod" in arguments:
                raise release.ReleaseError("Pod deletion failed")
            return SimpleNamespace(returncode=0, stdout="Synthetic image failed to import a required module\n" if "logs" in arguments else "")

        with patch.object(release, "run", side_effect=fake_run), \
             patch.object(release, "output", return_value=json.dumps({"status": {"phase": phase}})), \
             patch.object(release.time, "monotonic", side_effect=[0, 241] if timed_out else [0, 1]):
            if phase == "Failed" or timed_out or creation_failed or deletion_failed:
                with self.assertRaises(release.ReleaseError):
                    release.report_smoke(self.after)
            else:
                release.report_smoke(self.after)
        self.assertIn("apply", actions[0][0])
        self.assertIn("delete", actions[-1][0])
        self.assertIn("--ignore-not-found", actions[-1][0])
        self.assertEqual(actions[-1][1]["timeout"], 45)
        deletions = [action[0] for action in actions if "delete" in action[0]]
        self.assertIn("pod", deletions[0])
        if deletion_failed:
            self.assertEqual(len(deletions), 1)
        else:
            self.assertIn("networkpolicy", deletions[1])
            self.assertEqual(deletions[0][deletions[0].index("pod") + 1], deletions[1][deletions[1].index("networkpolicy") + 1])
        if phase == "Failed" or timed_out or creation_failed:
            self.assertTrue(any("logs" in action[0] for action in actions))
        return manifests[0]

    def test_smoke_uses_production_security_without_secrets_pvc_or_network(self):
        policy, pod = self.exercise_smoke()
        original = release.resources(self.after)["batch/v1", "CronJob", "systems", "daily-report"]["spec"]["jobTemplate"]["spec"]["template"]["spec"]
        self.assertEqual(pod["spec"]["securityContext"], original["securityContext"])
        self.assertEqual(pod["spec"]["imagePullSecrets"], original["imagePullSecrets"])
        self.assertFalse(pod["spec"]["automountServiceAccountToken"])
        self.assertNotIn("affinity", pod["spec"])
        self.assertEqual(pod["spec"]["activeDeadlineSeconds"], 180)
        self.assertEqual(pod["spec"]["restartPolicy"], "Never")
        self.assertTrue(all(volume == {"name": volume["name"], "emptyDir": {}} for volume in pod["spec"]["volumes"]))
        container = pod["spec"]["containers"][0]
        self.assertEqual(container["image"], IMAGE)
        self.assertEqual(container["command"], ["python", "-u", "deployment_smoke.py"])
        self.assertEqual(container["args"], [])
        self.assertEqual(container["securityContext"], original["containers"][0]["securityContext"])
        self.assertNotIn("envFrom", container)
        self.assertEqual({item["name"] for item in container["env"]}, {"DATA_DIR", "TZ", "PYTHONDONTWRITEBYTECODE", "PYTHONUNBUFFERED"})
        self.assertEqual(policy["spec"]["podSelector"]["matchLabels"], pod["metadata"]["labels"])
        self.assertEqual(policy["spec"]["egress"], [])
        self.assertEqual(set(policy["spec"]["policyTypes"]), {"Ingress", "Egress"})

    def test_smoke_cleans_up_on_failure_timeout_and_unknown_creation_outcome(self):
        self.exercise_smoke(phase="Failed")
        self.exercise_smoke(timed_out=True)
        self.exercise_smoke(creation_failed=True)

    def test_smoke_retains_egress_deny_when_pod_deletion_fails(self):
        self.exercise_smoke(deletion_failed=True)


if __name__ == "__main__":
    unittest.main()
