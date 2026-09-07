"""Check the report's scheduling, persistent state, and printer boundary."""
from pathlib import Path
import subprocess
import unittest

import yaml


ROOT = Path(__file__).resolve().parents[1]
IMAGE = "registry.digitalocean.com/freddierice-systems/daily-report@sha256:" + "a" * 64


def render(*arguments):
    return subprocess.run(
        ["helm", "template", "systems", str(ROOT / "kubernetes/charts/systems"),
         "--namespace", "systems", *arguments],
        text=True, capture_output=True,
    )


class DailyReportTests(unittest.TestCase):
    def report_resources(self, *arguments):
        result = render("--set", "dailyReport.enabled=true", "--set", f"dailyReport.image={IMAGE}", *arguments)
        self.assertEqual(result.returncode, 0, result.stderr)
        return {(r["kind"], r["metadata"]["name"]): r for r in yaml.safe_load_all(result.stdout) if r}

    def test_report_is_disabled_for_initial_provisioning(self):
        result = render()
        self.assertEqual(result.returncode, 0, result.stderr)
        resources = [r for r in yaml.safe_load_all(result.stdout) if r]
        self.assertFalse(any(r["metadata"]["name"].startswith("daily-report") for r in resources))

    def test_report_requires_the_docr_repository_and_immutable_digest(self):
        for image in ("", "daily-report:latest", IMAGE.replace("daily-report@", "health@"),
                      IMAGE.replace("registry.digitalocean.com", "example.com"), IMAGE[:-1]):
            with self.subTest(image=image):
                result = render("--set", "dailyReport.enabled=true", "--set", f"dailyReport.image={image}")
                self.assertNotEqual(result.returncode, 0)
                self.assertIn("immutable DOCR daily-report image digest", result.stderr)

    def test_schedule_starts_suspended_and_printing_is_not_retried(self):
        resources = self.report_resources()
        cron = resources["CronJob", "daily-report"]["spec"]
        self.assertTrue(cron["suspend"])
        self.assertEqual(cron["schedule"], "30 5 * * *")
        self.assertEqual(cron["timeZone"], "America/Chicago")
        self.assertEqual(cron["concurrencyPolicy"], "Forbid")
        self.assertEqual(cron["startingDeadlineSeconds"], 900)
        self.assertEqual(cron["successfulJobsHistoryLimit"], 3)
        self.assertEqual(cron["failedJobsHistoryLimit"], 3)
        job = cron["jobTemplate"]["spec"]
        self.assertEqual(job["backoffLimit"], 0)
        self.assertEqual(job["activeDeadlineSeconds"], 2400)
        pod = job["template"]["spec"]
        self.assertEqual(pod["restartPolicy"], "Never")
        self.assertEqual(pod["containers"][0]["args"], ["run"])
        active = self.report_resources("--set", "dailyReport.suspend=false")
        self.assertFalse(active["CronJob", "daily-report"]["spec"]["suspend"])

    def test_report_keeps_state_and_mounts_required_credentials(self):
        resources = self.report_resources()
        claim = resources["PersistentVolumeClaim", "daily-report-data"]
        self.assertEqual(claim["metadata"]["annotations"]["helm.sh/resource-policy"], "keep")
        self.assertEqual(claim["spec"]["storageClassName"], "do-block-storage-retain")
        self.assertEqual(claim["spec"]["accessModes"], ["ReadWriteOnce"])
        self.assertEqual(claim["spec"]["resources"]["requests"]["storage"], "1Gi")
        pod = resources["CronJob", "daily-report"]["spec"]["jobTemplate"]["spec"]["template"]["spec"]
        container = pod["containers"][0]
        self.assertEqual(container["envFrom"], [{"secretRef": {"name": "daily-report-runtime"}}])
        env = {entry["name"]: entry["value"] for entry in container["env"]}
        self.assertEqual(env["DATA_DIR"], "/data")
        self.assertEqual(env["TZ"], "America/Chicago")
        self.assertEqual(env["GDRIVE_CREDENTIALS"], "/etc/daily-report/google.json")
        volumes = {entry["name"]: entry for entry in pod["volumes"]}
        self.assertEqual(volumes["data"]["persistentVolumeClaim"]["claimName"], "daily-report-data")
        self.assertEqual(volumes["tmp"]["emptyDir"], {})
        self.assertEqual(volumes["google"]["secret"], {
            "secretName": "daily-report-google", "defaultMode": 0o440,
            "items": [{"key": "google.json", "path": "google.json"}],
        })
        mounts = {entry["name"]: entry for entry in container["volumeMounts"]}
        self.assertEqual(mounts["data"]["mountPath"], "/data")
        self.assertTrue(mounts["google"]["readOnly"])
        self.assertEqual(mounts["google"]["mountPath"], "/etc/daily-report")

    def test_report_runs_unprivileged_without_cluster_credentials(self):
        resources = self.report_resources("--set", "imagePullSecrets[0].name=freddierice-systems")
        self.assertFalse(resources["ServiceAccount", "daily-report"]["automountServiceAccountToken"])
        pod = resources["CronJob", "daily-report"]["spec"]["jobTemplate"]["spec"]["template"]["spec"]
        self.assertEqual(pod["serviceAccountName"], "daily-report")
        self.assertFalse(pod["automountServiceAccountToken"])
        self.assertEqual(pod["imagePullSecrets"], [{"name": "freddierice-systems"}])
        self.assertTrue(pod["securityContext"]["runAsNonRoot"])
        self.assertEqual(pod["securityContext"]["runAsUser"], 1000)
        self.assertEqual(pod["securityContext"]["fsGroup"], 1000)
        security = pod["containers"][0]["securityContext"]
        self.assertFalse(security["allowPrivilegeEscalation"])
        self.assertTrue(security["readOnlyRootFilesystem"])
        self.assertEqual(security["capabilities"]["drop"], ["ALL"])

    def test_printer_service_is_explicitly_enabled_and_private(self):
        disabled = self.report_resources()
        self.assertNotIn(("Service", "daily-report-printer"), disabled)
        self.assertNotIn(("ProxyClass", "daily-report-printer"), disabled)
        resources = self.report_resources("--set", "dailyReport.printer.enabled=true")
        service = resources["Service", "daily-report-printer"]
        self.assertEqual(service["spec"]["type"], "ExternalName")
        self.assertEqual(service["metadata"]["annotations"], {
            "tailscale.com/tailnet-ip": "10.0.0.33", "tailscale.com/tags": "tag:systems",
            "tailscale.com/proxy-class": "daily-report-printer",
        })
        proxy_class = resources["ProxyClass", service["metadata"]["annotations"]["tailscale.com/proxy-class"]]
        self.assertEqual(proxy_class["apiVersion"], "tailscale.com/v1alpha1")
        self.assertNotIn("namespace", proxy_class["metadata"])
        self.assertTrue(proxy_class["spec"]["tailscale"]["acceptRoutes"])
        self.assertEqual(service["spec"]["ports"], [{"name": "ipp", "port": 631, "protocol": "TCP"}])
        pod = resources["CronJob", "daily-report"]["spec"]["jobTemplate"]["spec"]["template"]["spec"]
        env = {entry["name"]: entry["value"] for entry in pod["containers"][0]["env"]}
        self.assertEqual(env["PRINTER_URI"], "ipp://daily-report-printer.systems.svc.cluster.local/ipp/print")


if __name__ == "__main__":
    unittest.main()
