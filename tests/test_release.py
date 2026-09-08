"""Exercise release boundaries and failure recovery without cloud credentials."""
import copy
import importlib.util
import io
import json
from pathlib import Path
import subprocess
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import MagicMock, patch

import yaml

ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location("deploy_app", ROOT / "scripts/deploy-app.py")
release = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(release)
SHA = "a" * 40
DIGEST = "sha256:" + "b" * 64
IMAGE = f"registry.digitalocean.com/freddierice-systems/health@{DIGEST}"
VALUES = (ROOT / release.VALUES).read_text()
OLD_IMAGE = yaml.safe_load(VALUES)["apps"]["health"]["image"]


def git(repo, *arguments):
    return subprocess.check_output(["git", "-C", str(repo), *arguments], text=True, stderr=subprocess.DEVNULL).strip()


def commit(repo, message):
    git(repo, "add", ".")
    git(repo, "-c", "user.name=Test", "-c", "user.email=test@example.com", "commit", "-m", message)
    return git(repo, "rev-parse", "HEAD")


class ValidationTests(unittest.TestCase):
    def test_source_auth_clears_checkout_header_without_changing_push_auth(self):
        token = "test-source-token-never-in-arguments"
        with patch.dict(release.os.environ, {"GH_TOKEN": token}, clear=True), \
             patch.object(release.subprocess, "run", return_value=SimpleNamespace(returncode=0, stdout="")) as execute:
            release.run("git", "ls-remote", release.REPOSITORIES["health"], "refs/heads/main", source_auth=True)
            release.run("git", "push", "origin", "HEAD:main")
        source, push = execute.call_args_list
        source_env, push_env = source.kwargs["env"], push.kwargs["env"]
        self.assertEqual(source_env["GIT_CONFIG_COUNT"], "3")
        self.assertEqual(source_env["GIT_CONFIG_KEY_2"], "http.https://github.com/.extraheader")
        self.assertEqual(source_env["GIT_CONFIG_VALUE_2"], "")
        self.assertEqual(source_env["GIT_CONFIG_VALUE_1"], "!gh auth git-credential")
        self.assertNotIn("GIT_CONFIG_COUNT", push_env)
        for call in (source, push):
            self.assertNotIn(token, " ".join(call.args[0]))
        self.assertEqual(source_env["GH_TOKEN"], token)

    def test_rejects_payloads_before_any_external_command(self):
        for args in (("other", SHA, DIGEST, "1"), ("health", "main", DIGEST, "1"),
                     ("health", SHA, "latest", "1"), ("health", SHA, DIGEST, "1;echo bad")):
            with self.subTest(args=args), self.assertRaises(release.ReleaseError), patch.object(release, "run") as run:
                release.validate_inputs(*args)
            run.assert_not_called()

    def test_exact_registry_tag_digest_required(self):
        with patch.object(release, "output", return_value=json.dumps([
            {"registry_name": "freddierice-systems", "repository": "health",
             "manifest_digest": "sha256:" + "d" * 64, "compressed_size_bytes": 1024,
             "size_bytes": 2048, "updated_at": "2026-09-07T20:59:00Z"},
            {"tag": SHA, "manifest_digest": DIGEST},
            {"tag": "latest", "manifest_digest": "sha256:" + "c" * 64},
        ])):
            release.verify_digest("health", SHA, DIGEST)
            with self.assertRaises(release.ReleaseError):
                release.verify_digest("health", "f" * 40, DIGEST)
            with self.assertRaises(release.ReleaseError):
                release.verify_digest("health", SHA, "sha256:" + "c" * 64)

    def test_updates_only_selected_application(self):
        original = "# Keep this comment.\n" + VALUES
        updated, old = release.update_values(original, "health", IMAGE, SHA)
        before, after = yaml.safe_load(original), yaml.safe_load(updated)
        self.assertEqual(before["apps"]["trends"], after["apps"]["trends"])
        self.assertEqual(after["apps"]["health"]["image"], IMAGE)
        self.assertEqual(after["apps"]["health"]["sourceRevision"], SHA)
        self.assertEqual(old, before["apps"]["health"])
        self.assertTrue(updated.startswith("# Keep this comment.\n"))
        numeric, _ = release.update_values(original, "health", IMAGE, "1" * 40)
        self.assertEqual(yaml.safe_load(numeric)["apps"]["health"]["sourceRevision"], "1" * 40)
        with self.assertRaises(release.ReleaseError):
            release.update_values(original.replace("    sourceRevision:", "    unknownRevision:"), "health", IMAGE, SHA)

    def test_migration_tree_changes_require_explicit_release(self):
        with tempfile.TemporaryDirectory() as temporary:
            repo = Path(temporary)
            git(repo, "init", "-b", "main")
            (repo / "health/migrations").mkdir(parents=True)
            (repo / "health/migrations/001.sql").write_text("CREATE TABLE example(id integer);\n")
            old = commit(repo, "Initial schema")
            (repo / "README.md").write_text("Documentation only\n")
            current = commit(repo, "Documentation")
            release.verify_schema("health", old, current, repo)
            (repo / "health/migrations/002.sql").write_text("ALTER TABLE example ADD name text;\n")
            changed = commit(repo, "Schema update")
            with self.assertRaisesRegex(release.ReleaseError, "Migration files changed"):
                release.verify_schema("health", old, changed, repo)
            with patch.object(release, "output") as query:
                release.verify_schema("health", old, changed, repo, schema_verified=True)
                query.assert_not_called()
            with self.assertRaises(release.ReleaseError):
                release.verify_schema("health", None, changed, repo)

    def test_runner_cleanup_allows_repeated_updates_but_sql_changes_still_stop(self):
        for app in ("health", "trends"):
            with self.subTest(app=app), tempfile.TemporaryDirectory() as temporary:
                repo = Path(temporary)
                git(repo, "init", "-b", "main")
                migrations = repo / app / "migrations"
                migrations.mkdir(parents=True)
                schema = migrations / "001.sql"
                schema.write_text("CREATE TABLE example(id integer);\n")
                runner = repo / app / "migrate.py"
                runner.write_text("# Legacy SQLite importer and PostgreSQL runner\n")
                old = commit(repo, "Initial schema and runner")
                runner.write_text("# PostgreSQL runner only\n")
                current = commit(repo, "Retire SQLite importer")
                release.verify_schema(app, old, current, repo)
                (repo / "README.md").write_text("Documentation\n")
                current = commit(repo, "Another update before successful release")
                release.verify_schema(app, old, current, repo)
                schema.write_text("CREATE TABLE example(id bigint);\n")
                changed = commit(repo, "Edit existing migration")
                with self.assertRaisesRegex(release.ReleaseError, "Migration files changed"):
                    release.verify_schema(app, old, changed, repo)
                schema.rename(migrations / "002.sql")
                renamed = commit(repo, "Rename migration")
                with self.assertRaisesRegex(release.ReleaseError, "Migration files changed"):
                    release.verify_schema(app, changed, renamed, repo)

    def test_database_free_exception_does_not_allow_removing_app_migrations(self):
        for app in ("health", "trends"):
            with self.subTest(app=app), tempfile.TemporaryDirectory() as temporary:
                repo = Path(temporary)
                git(repo, "init", "-b", "main")
                migrations = repo / app / "migrations"
                migrations.mkdir(parents=True)
                schema = migrations / "001.sql"
                schema.write_text("CREATE TABLE example(id integer);\n")
                old = commit(repo, "Initial schema")
                schema.unlink()
                (repo / "README.md").write_text("Documentation\n")
                current = commit(repo, "Remove schema files")
                with self.assertRaisesRegex(release.ReleaseError, "Migration files changed"):
                    release.verify_schema(app, old, current, repo)
                with self.assertRaisesRegex(release.ReleaseError, "Migration files changed"):
                    release.verify_schema(app, current, current, repo)


class DeploymentTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.before = subprocess.check_output([
            "helm", "template", "systems", str(ROOT / release.CHART), "--namespace", "systems",
            "--values", str(ROOT / release.VALUES),
        ], text=True)
        cls.old_image = OLD_IMAGE
        cls.after = cls.before.replace(cls.old_image, IMAGE)

    def test_other_app_and_platform_changes_are_rejected(self):
        self.assertFalse(release.image_only(self.before, self.after, "health", self.old_image, IMAGE))
        self.assertTrue(release.image_only(self.after, self.after, "health", self.old_image, IMAGE))
        documents = list(yaml.safe_load_all(self.after))
        for name in ("trends", "systems-dns"):
            changed = copy.deepcopy(documents)
            next(item for item in changed if item["kind"] == "Deployment" and item["metadata"]["name"] == name)["spec"]["replicas"] = 9
            with self.subTest(name=name), self.assertRaises(release.ReleaseError):
                release.image_only(self.before, yaml.safe_dump_all(changed), "health", self.old_image, IMAGE)
        with self.assertRaises(release.ReleaseError):
            release.image_only(self.before, yaml.safe_dump_all(documents[:-1]), "health", self.old_image, IMAGE)

    def test_live_scaling_is_not_silently_reset(self):
        live = {"items": [item for item in yaml.safe_load_all(self.before) if item["kind"] == "Deployment"]}
        with patch.object(release, "output", return_value=json.dumps(live)):
            release.verify_replicas(self.before)
        live["items"][0]["spec"]["replicas"] = 0
        with patch.object(release, "output", return_value=json.dumps(live)), self.assertRaises(release.ReleaseError):
            release.verify_replicas(self.before)

    def exercise(self, *, failure=None, dry_run=False, stale=False, unchanged=False, schema_verified=False):
        with tempfile.TemporaryDirectory() as temporary:
            repo = Path(temporary)
            values = repo / release.VALUES
            values.parent.mkdir(parents=True)
            values.write_text(VALUES)
            args = SimpleNamespace(app="health", source_sha=SHA, image_digest=DIGEST, run_id="123",
                                   dry_run=dry_run, schema_verified=schema_verified)

            def fake_output(*arguments, **kwargs):
                if arguments[0:2] == ("helm", "template"):
                    return self.after
                if "status" in arguments and arguments[0] == "helm":
                    return json.dumps({"version": 8, "info": {"status": "deployed"}})
                if "manifest" in arguments:
                    return self.after if unchanged else self.before
                if "rev-parse" in arguments:
                    return "c" * 40
                return ""

            def fake_run(*arguments, **kwargs):
                if "upgrade" in arguments and failure == "helm":
                    raise release.ReleaseError("Upgrade failed")
                return SimpleNamespace(returncode=0)

            with patch.object(release, "ROOT", repo), patch.object(release, "current_main", return_value="f" * 40 if stale else SHA), \
                 patch.object(release, "verify_digest", side_effect=release.ReleaseError("Wrong digest") if failure == "digest" else None) as digest, \
                 patch.object(release, "verify_schema") as schema, \
                 patch.object(release, "verify_replicas"), patch.object(release, "output", side_effect=fake_output), \
                 patch.object(release, "run", side_effect=fake_run) as commands, \
                 patch.object(release, "smoke", side_effect=release.ReleaseError("Smoke failed") if failure == "smoke" else None), \
                 patch.object(release, "record_release") as record:
                if failure:
                    with self.assertRaises(release.ReleaseError):
                        release.deploy(args)
                else:
                    release.deploy(args)
                calls = [call.args for call in commands.call_args_list]
                if failure or dry_run or stale:
                    self.assertEqual(values.read_text(), VALUES)
                    record.assert_not_called()
                else:
                    self.assertEqual(yaml.safe_load(values.read_text())["apps"]["health"]["image"], IMAGE)
                    record.assert_called_once_with("health", SHA, "123")
                if stale:
                    digest.assert_not_called()
                else:
                    digest.assert_called_once_with("health", SHA, DIGEST)
                if stale or failure == "digest":
                    schema.assert_not_called()
                else:
                    self.assertEqual(schema.call_args.kwargs["schema_verified"], schema_verified)
                self.assertEqual(any("rollback" in call for call in calls), failure == "smoke" and not unchanged)
                self.assertEqual(any("upgrade" in call for call in calls), not (dry_run or stale or unchanged or failure == "digest"))
                if failure == "smoke":
                    rollback = next(call for call in calls if "rollback" in call)
                    self.assertIn("8", rollback)

    def test_helm_failure_does_not_record_success(self):
        self.exercise(failure="helm")

    def test_smoke_failure_rolls_back_previous_release(self):
        self.exercise(failure="smoke")

    def test_dry_run_never_upgrades_or_commits(self):
        self.exercise(dry_run=True)

    def test_stale_source_is_skipped(self):
        self.exercise(stale=True)

    def test_success_records_release(self):
        self.exercise()

    def test_retry_after_failed_git_push_records_without_redeploying(self):
        self.exercise(unchanged=True)

    def test_operator_schema_confirmation_keeps_digest_and_main_guards(self):
        self.exercise(schema_verified=True)
        self.exercise(schema_verified=True, failure="digest")
        self.exercise(schema_verified=True, stale=True)


class GatewaySmokeTests(unittest.TestCase):
    def test_tls_uses_application_sni_and_host_over_local_connection(self):
        context, connection, transport = MagicMock(), MagicMock(), MagicMock()
        connection.getresponse.return_value.status = 200
        connection.getresponse.return_value.getheader.return_value = None
        with patch.object(release.socket, "create_connection") as connect, \
             patch.object(release.http.client, "HTTPConnection", return_value=connection):
            connect.return_value.__enter__.return_value = transport
            self.assertEqual(release.tls_get("health.freddie.xyz", 40123, "/ready", context), (200, None))
        connect.assert_called_once_with(("127.0.0.1", 40123), timeout=5)
        context.wrap_socket.assert_called_once_with(transport, server_hostname="health.freddie.xyz")
        connection.request.assert_called_once_with("GET", "/ready", headers={"Host": "health.freddie.xyz", "Connection": "close"})
        connection.close.assert_called_once()

    def test_port_forward_reaps_process_when_smoke_fails(self):
        process = MagicMock()
        process.stdout = io.StringIO("Forwarding from 127.0.0.1:40123 -> 443\n")
        process.poll.return_value = None
        process.wait.side_effect = [subprocess.TimeoutExpired("kubectl", 5), 0]
        with patch.object(release.subprocess, "Popen", return_value=process) as popen, \
             patch.object(release.selectors, "DefaultSelector") as selector:
            selector.return_value.__enter__.return_value.select.return_value = [(None, None)]
            with self.assertRaisesRegex(release.ReleaseError, "failed"):
                with release.gateway_forward() as port:
                    self.assertEqual(port, 40123)
                    raise release.ReleaseError("Page check failed")
        command = popen.call_args.args[0]
        self.assertEqual(command[-4:], ["--address", "127.0.0.1", "service/systems-gateway", ":443"])
        self.assertIn("systems", command)
        process.terminate.assert_called_once()
        process.kill.assert_called_once()
        self.assertEqual(process.wait.call_count, 2)
        self.assertTrue(process.stdout.closed)

    def test_port_forward_startup_timeout_cleans_up(self):
        process = MagicMock()
        process.poll.return_value = None
        with patch.object(release.subprocess, "Popen", return_value=process), \
             patch.object(release.selectors, "DefaultSelector"), \
             patch.object(release.time, "monotonic", side_effect=[0, 31]):
            with self.assertRaisesRegex(release.ReleaseError, "30 seconds"):
                with release.gateway_forward():
                    self.fail("Port-forward was never ready")
        process.terminate.assert_called_once()
        process.wait.assert_called_once_with(timeout=5)
        process.stdout.close.assert_called_once()

    def test_health_checks_expected_root_redirect_and_destination(self):
        with patch.object(release, "gateway_forward") as forward, patch.object(release, "tls_get") as get:
            forward.return_value.__enter__.return_value = 40123
            get.side_effect = [(200, None), (307, "/workouts"), (200, None)]
            release.smoke("health")
        self.assertEqual([call.args[2] for call in get.call_args_list], ["/ready", "/", "/workouts"])
        context = get.call_args.args[3]
        self.assertTrue(context.check_hostname)
        self.assertEqual(context.verify_mode, release.ssl.CERT_REQUIRED)
        forward.return_value.__exit__.assert_called_once()

    def test_failed_page_prevents_success_even_when_readiness_passes(self):
        with patch.object(release, "gateway_forward") as forward, patch.object(release, "tls_get") as get, \
             patch.object(release.time, "sleep"):
            forward.return_value.__enter__.return_value = 40123
            get.side_effect = [(200, None), (503, None)] * 6
            with self.assertRaisesRegex(release.ReleaseError, "application-page checks failed"):
                release.smoke("trends")
        self.assertEqual(get.call_count, 12)
        forward.return_value.__exit__.assert_called_once()


class GitRecordingTests(unittest.TestCase):
    def exercise(self, conflicting):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            remote, checkout, peer = root / "remote.git", root / "checkout", root / "peer"
            remote.mkdir()
            git(remote, "init", "--bare", "-b", "main")
            git(root, "clone", str(remote), str(checkout))
            (checkout / release.VALUES).parent.mkdir(parents=True)
            (checkout / release.VALUES).write_text(VALUES)
            commit(checkout, "Production baseline")
            git(checkout, "push", "origin", "main")
            git(root, "clone", str(remote), str(peer))
            if conflicting:
                (peer / release.VALUES).write_text(VALUES + "# Concurrent configuration change\n")
            else:
                (peer / "README.md").write_text("Concurrent documentation change\n")
            commit(peer, "Concurrent update")
            git(peer, "push", "origin", "main")
            updated, _ = release.update_values(VALUES, "health", IMAGE, SHA)
            (checkout / release.VALUES).write_text(updated)
            with patch.object(release, "ROOT", checkout):
                if conflicting:
                    with self.assertRaisesRegex(release.ReleaseError, "configuration changed upstream"):
                        release.record_release("health", SHA, "123")
                else:
                    release.record_release("health", SHA, "123")
            stored = yaml.safe_load(git(remote, "show", f"main:{release.VALUES}"))
            self.assertEqual(stored["apps"]["health"]["image"], OLD_IMAGE if conflicting else IMAGE)
            if not conflicting:
                self.assertIn("Concurrent documentation", git(remote, "show", "main:README.md"))

    def test_retries_unrelated_upstream_change_without_force_push(self):
        self.exercise(conflicting=False)

    def test_does_not_overwrite_concurrent_production_change(self):
        self.exercise(conflicting=True)


if __name__ == "__main__":
    unittest.main()
