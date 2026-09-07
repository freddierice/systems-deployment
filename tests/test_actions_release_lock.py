"""Verify GCS release-lock races and recovery without cloud access."""
import importlib.util
import io
import json
from pathlib import Path
import stat
import tempfile
import unittest
from unittest.mock import patch
from urllib.parse import parse_qs, urlsplit
import urllib.error

ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location("release_lock", ROOT / "scripts/actions-release-lock.py")
LOCK = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(LOCK)
OWNER = "freddierice/health/123/1"
OTHER = "freddierice/trends/456/1"


def held(owner=OWNER, generation="100", expires=10000):
    return {"owner": owner, "generation": generation, "expires_at": expires}


class ReleaseLockTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.state = Path(self.temporary.name) / "lock.json"
        clock = patch.object(LOCK.time, "time", return_value=1000)
        clock.start()
        self.addCleanup(clock.stop)

    def test_create_uses_no_live_generation_precondition_and_private_pending_state(self):
        def upload(method, url, token, data):
            self.assertEqual(method, "POST")
            self.assertEqual(parse_qs(urlsplit(url).query)["ifGenerationMatch"], ["0"])
            state = LOCK.read_state(self.state, OWNER)
            self.assertIsNone(state["generation"])
            self.assertEqual(state["owner"], OWNER)
            self.assertEqual(stat.S_IMODE(self.state.stat().st_mode), 0o600)
            self.assertEqual(data, {"owner": OWNER, "expires_at": 1000 + LOCK.LEASE_SECONDS})
            return {"generation": "100"}

        with patch.object(LOCK, "current_lock", return_value=None), patch.object(LOCK, "request", side_effect=upload):
            LOCK.acquire("secret", self.state, OWNER, wait_seconds=0)
        self.assertEqual(LOCK.read_state(self.state, OWNER)["generation"], "100")

    def test_contender_does_not_replace_active_holder(self):
        with patch.object(LOCK, "current_lock", return_value=held(OTHER)), patch.object(LOCK, "request") as api:
            with self.assertRaisesRegex(RuntimeError, "Timed out"):
                LOCK.acquire("secret", self.state, OWNER, wait_seconds=0)
        api.assert_not_called()
        self.assertFalse(self.state.exists())

    def test_expired_holder_is_replaced_with_its_exact_generation(self):
        with patch.object(LOCK, "current_lock", return_value=held(OTHER, "99", expires=999)):
            with patch.object(LOCK, "request", return_value={"generation": "101"}) as api:
                LOCK.acquire("secret", self.state, OWNER, wait_seconds=0)
        self.assertEqual(parse_qs(urlsplit(api.call_args.args[1]).query)["ifGenerationMatch"], ["99"])

    def test_lost_upload_response_recovers_existing_own_lock(self):
        with patch.object(LOCK, "current_lock", side_effect=[None, held()]), \
             patch.object(LOCK, "request", side_effect=LOCK.APIError("POST", 412)), \
             patch.object(LOCK.time, "sleep"):
            LOCK.acquire("secret", self.state, OWNER, wait_seconds=30)
        self.assertEqual(LOCK.read_state(self.state, OWNER)["generation"], "100")

    def test_conditional_race_rechecks_holder_before_uploading_again(self):
        with patch.object(LOCK, "current_lock", side_effect=[held(OTHER, expires=999), held(OTHER), None]), \
             patch.object(LOCK, "request", side_effect=[LOCK.APIError("POST", 412), {"generation": "102"}]) as api, \
             patch.object(LOCK.time, "sleep"):
            LOCK.acquire("secret", self.state, OWNER, wait_seconds=30)
        self.assertEqual([parse_qs(urlsplit(call.args[1]).query)["ifGenerationMatch"] for call in api.call_args_list], [["100"], ["0"]])

    def test_release_deletes_only_recorded_own_generation(self):
        LOCK.save_state(self.state, OWNER, 10000, "100")
        with patch.object(LOCK, "current_lock", return_value=held()), patch.object(LOCK, "request", return_value=None) as api:
            LOCK.release("secret", self.state, OWNER)
        self.assertEqual(api.call_args.args[0], "DELETE")
        self.assertEqual(parse_qs(urlsplit(api.call_args.args[1]).query), {"ifGenerationMatch": ["100"]})
        self.assertFalse(self.state.exists())

    def test_release_cannot_delete_new_owner_or_new_generation(self):
        for current in (held(OTHER, "101"), held(OWNER, "101")):
            with self.subTest(current=current):
                LOCK.save_state(self.state, OWNER, 10000, "100")
                with patch.object(LOCK, "current_lock", return_value=current), patch.object(LOCK, "request") as api:
                    LOCK.release("secret", self.state, OWNER)
                api.assert_not_called()
                self.assertFalse(self.state.exists())

    def test_pending_state_can_release_upload_accepted_before_process_died(self):
        LOCK.save_state(self.state, OWNER, 10000)
        with patch.object(LOCK, "current_lock", return_value=held()), patch.object(LOCK, "request", return_value=None) as api:
            LOCK.release("secret", self.state, OWNER)
        self.assertEqual(parse_qs(urlsplit(api.call_args.args[1]).query)["ifGenerationMatch"], ["100"])

    def test_delete_generation_race_leaves_the_replacement(self):
        LOCK.save_state(self.state, OWNER, 10000, "100")
        with patch.object(LOCK, "current_lock", side_effect=[held(), held(OTHER, "101")]), \
             patch.object(LOCK, "request", side_effect=LOCK.APIError("DELETE", 412)) as api:
            LOCK.release("secret", self.state, OWNER)
        self.assertEqual(api.call_count, 1)
        self.assertFalse(self.state.exists())

    def test_failed_release_keeps_state_for_retry(self):
        LOCK.save_state(self.state, OWNER, 10000, "100")
        with patch.object(LOCK, "current_lock", return_value=held()), \
             patch.object(LOCK, "request", side_effect=LOCK.APIError("DELETE", 401)):
            with self.assertRaises(LOCK.APIError):
                LOCK.release("expired-token", self.state, OWNER)
        self.assertTrue(self.state.exists())

    def test_release_without_state_is_a_read_only_noop(self):
        with patch.object(LOCK, "request") as api:
            LOCK.release(None, self.state, OWNER)
        api.assert_not_called()

    def test_read_locks_pins_content_to_metadata_generation(self):
        with patch.object(LOCK, "request", side_effect=[{"generation": "100"}, {"owner": OWNER, "expires_at": 10000}]) as api:
            self.assertEqual(LOCK.current_lock("secret"), held())
        self.assertEqual(parse_qs(urlsplit(api.call_args.args[1]).query), {"alt": ["media"], "ifGenerationMatch": ["100"]})

    def test_request_retries_transient_errors_without_logging_token(self):
        error = urllib.error.HTTPError(LOCK.BASE, 503, "secret-token", {}, io.BytesIO(b"secret-token"))
        with patch.object(LOCK.urllib.request, "urlopen", side_effect=[error, io.BytesIO(b'{"generation":"100"}')]) as http, \
             patch.object(LOCK.time, "sleep"):
            self.assertEqual(LOCK.request("GET", LOCK.BASE, "secret-token"), {"generation": "100"})
        self.assertEqual(http.call_count, 2)
        self.assertNotIn("secret-token", str(LOCK.APIError("GET", 503)))

    def test_owner_rejects_non_ascii_missing_attempt_and_shell_characters(self):
        for owner in ("freddierice/health/123", "freddierice/health/123/0", "freddierice/health/1/1;ls", "fréddie/health/1/1"):
            with self.subTest(owner=owner), self.assertRaises(ValueError):
                LOCK.valid_owner(owner)


if __name__ == "__main__":
    unittest.main()
