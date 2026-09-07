"""Check temporary DOKS firewall access without making network requests."""
import importlib.util
import io
import json
from pathlib import Path
import stat
import tempfile
import unittest
from unittest.mock import patch
import urllib.error


ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location("cluster_access", ROOT / "scripts/actions-cluster-access.py")
ACCESS = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(ACCESS)
RUNNER = "8.8.8.8/32"
ADMIN = "1.1.1.1/32"
OTHER = "9.9.9.9/32"


def cluster(allowed, enabled=True, status="running"):
    return {"id": ACCESS.CLUSTER_ID, "endpoint": "https://example.k8s.ondigitalocean.com",
            "status": {"state": status},
            "control_plane_firewall": {"enabled": enabled, "allowed_addresses": allowed}}


class FirewallAccessTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.state = Path(self.temporary.name) / "access.json"

    def save_state(self, added=True):
        ACCESS.save_state(self.state, {"cluster_id": ACCESS.CLUSTER_ID, "cidr": RUNNER, "added": added})

    def test_prepare_persists_private_cleanup_state_before_mutation(self):
        def api(method, token, data=None):
            if method == "GET":
                return cluster([ADMIN])
            self.assertEqual(json.loads(self.state.read_text())["added"], True)
            self.assertEqual(stat.S_IMODE(self.state.stat().st_mode), 0o600)
            log.assert_called_with(f"Runner public IPv4 allowance: {RUNNER}", flush=True)
            self.assertEqual(data, {"control_plane_firewall": {
                "enabled": True, "allowed_addresses": [ADMIN, RUNNER]}})
            raise RuntimeError("Lost response after firewall update")

        with patch.object(ACCESS, "request", side_effect=api), patch("builtins.print") as log:
            with self.assertRaisesRegex(RuntimeError, "Lost response"):
                ACCESS.prepare("secret", self.state, "8.8.8.8")
        self.assertTrue(self.state.exists())

    def test_cleanup_removes_only_own_address_from_current_configuration(self):
        self.save_state()
        with patch.object(ACCESS, "request", side_effect=[cluster([ADMIN, RUNNER, OTHER]), cluster([ADMIN, OTHER])]) as api:
            with patch.object(ACCESS, "wait_for_change"):
                ACCESS.cleanup("secret", self.state)
        self.assertEqual(api.call_args.args[2], {"control_plane_firewall": {
            "enabled": True, "allowed_addresses": [ADMIN, OTHER]}})
        self.assertFalse(self.state.exists())

    def test_preexisting_address_is_neither_added_nor_removed(self):
        with patch.object(ACCESS, "request", return_value=cluster([ADMIN, "8.8.8.8"])) as api:
            with patch.object(ACCESS, "wait_for_change"):
                ACCESS.prepare("secret", self.state, "8.8.8.8")
                self.assertFalse(ACCESS.read_state(self.state)["added"])
                ACCESS.cleanup("secret", self.state)
        self.assertEqual([call.args[0] for call in api.call_args_list], ["GET"])

    def test_retry_rereads_addresses_instead_of_replaying_stale_update(self):
        with patch.object(ACCESS, "request", side_effect=[
            cluster([ADMIN]), ACCESS.RetryableError("rate limited", 1),
            cluster([ADMIN, OTHER]), cluster([ADMIN, OTHER, RUNNER]),
        ]) as api, patch.object(ACCESS.time, "sleep"):
            ACCESS.update_address("secret", RUNNER, adding=True)
        self.assertEqual([call.args[0] for call in api.call_args_list], ["GET", "PUT", "GET", "PUT"])
        self.assertEqual(api.call_args.args[2]["control_plane_firewall"]["allowed_addresses"], [ADMIN, OTHER, RUNNER])

    def test_uncertain_update_retries_without_duplicate_address(self):
        with patch.object(ACCESS, "request", side_effect=[
            cluster([ADMIN]), ACCESS.RetryableError("lost response"), cluster([ADMIN, RUNNER]),
        ]) as api, patch.object(ACCESS.time, "sleep"):
            ACCESS.update_address("secret", RUNNER, adding=True)
        self.assertEqual([call.args[0] for call in api.call_args_list], ["GET", "PUT", "GET"])

    def test_disabled_firewall_is_not_reconfigured(self):
        with patch.object(ACCESS, "request", return_value=cluster([ADMIN], enabled=False)) as api:
            with self.assertRaisesRegex(RuntimeError, "already be enabled"):
                ACCESS.prepare("secret", self.state, "8.8.8.8")
        self.assertEqual(api.call_count, 1)
        self.assertFalse(self.state.exists())

    def test_cleanup_is_safe_to_repeat_or_when_prepare_did_not_run(self):
        with patch.object(ACCESS, "request") as api:
            ACCESS.cleanup(None, self.state)
            api.assert_not_called()
        self.save_state()
        with patch.object(ACCESS, "request", return_value=cluster([ADMIN])) as api:
            with patch.object(ACCESS, "wait_for_change"):
                ACCESS.cleanup("secret", self.state)
                ACCESS.cleanup(None, self.state)
        self.assertEqual([call.args[0] for call in api.call_args_list], ["GET", "PUT"])
        self.assertEqual(api.call_args.args[2]["control_plane_firewall"]["allowed_addresses"], [ADMIN])

    def test_cleanup_failure_retains_state_for_retry(self):
        self.save_state()
        with patch.object(ACCESS, "request", return_value=cluster([ADMIN])):
            with patch.object(ACCESS, "wait_for_change", side_effect=RuntimeError("timeout")):
                with self.assertRaisesRegex(RuntimeError, "timeout"):
                    ACCESS.cleanup("secret", self.state)
        self.assertTrue(self.state.exists())

    def test_dry_run_changes_neither_remote_firewall_nor_state(self):
        with patch.object(ACCESS, "request", return_value=cluster([ADMIN])) as api:
            ACCESS.prepare("secret", self.state, "8.8.8.8", dry_run=True)
        self.assertTrue(all(call.args[0] == "GET" for call in api.call_args_list))
        self.assertFalse(self.state.exists())

    def test_runner_address_rejects_private_ipv6_and_cidr_input(self):
        for value in ("127.0.0.1", "10.0.0.1", "100.64.0.1", "::1", "8.8.8.8/24", "224.0.0.1", "example.com"):
            with self.subTest(value=value), self.assertRaises(ValueError):
                ACCESS.runner_cidr(value)

    def test_discovery_uses_ipify_without_digitalocean_credentials(self):
        with patch.object(ACCESS.urllib.request, "urlopen", return_value=io.BytesIO(b"8.8.8.8\n")) as http:
            self.assertEqual(ACCESS.runner_cidr(), RUNNER)
        self.assertEqual(http.call_args.args, ("https://api.ipify.org",))

    def test_wait_checks_address_status_and_new_api_connection(self):
        with patch.object(ACCESS, "get_cluster", side_effect=[
            cluster([ADMIN]), cluster([ADMIN, RUNNER], status="updating"), cluster([ADMIN, RUNNER]),
        ]), patch.object(ACCESS.time, "sleep"), patch.object(ACCESS.socket, "create_connection") as connect:
            ACCESS.wait_for_change("secret", RUNNER, present=True, timeout=10)
        connect.assert_called_once_with(("example.k8s.ondigitalocean.com", 443), timeout=5)

    def test_wait_timeout_is_bounded_and_actionable(self):
        with patch.object(ACCESS.time, "monotonic", side_effect=[0, 2]):
            with self.assertRaisesRegex(RuntimeError, "Timed out.*cleanup state retained"):
                ACCESS.wait_for_change("secret", RUNNER, present=True, timeout=1)

    def test_http_rate_limit_retries_without_echoing_secret_or_body(self):
        error = urllib.error.HTTPError(ACCESS.CLUSTER_URL, 429, "secret", {"Retry-After": "3"}, io.BytesIO(b"secret"))
        with patch.object(ACCESS.urllib.request, "urlopen", side_effect=error):
            with self.assertRaises(ACCESS.RetryableError) as caught:
                ACCESS.request("GET", "secret")
        self.assertEqual(caught.exception.delay, 3)
        self.assertNotIn("secret", str(caught.exception))


if __name__ == "__main__":
    unittest.main()
