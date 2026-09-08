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
        self.assertEqual(env["HEALTH_API_URL"], "http://health.systems.svc.cluster.local:8000")
        self.assertEqual(env["HEALTH_API_HOST"], "health.freddie.xyz")
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

    def test_report_health_route_can_use_an_https_endpoint_without_proxy_headers(self):
        resources = self.report_resources("--set", "dailyReport.health.url=https://health.freddie.xyz",
                                          "--set", "dailyReport.health.host=")
        pod = resources["CronJob", "daily-report"]["spec"]["jobTemplate"]["spec"]["template"]["spec"]
        env = {entry["name"]: entry["value"] for entry in pod["containers"][0]["env"]}
        self.assertEqual(env["HEALTH_API_URL"], "https://health.freddie.xyz")
        self.assertNotIn("HEALTH_API_HOST", env)

    def test_only_report_pods_receive_internal_health_access(self):
        resources = self.report_resources()
        policy = resources["NetworkPolicy", "daily-report-to-health"]["spec"]
        self.assertEqual(policy, {
            "podSelector": {"matchLabels": {"app.kubernetes.io/name": "health"}},
            "policyTypes": ["Ingress"],
            "ingress": [{
                "from": [{"podSelector": {"matchLabels": {"app.kubernetes.io/name": "daily-report"}}}],
                "ports": [{"port": 8000, "protocol": "TCP"}],
            }],
        })

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


class TimeViewerTests(unittest.TestCase):
    report_resources = DailyReportTests.report_resources
    def test_viewer_shares_report_image_and_only_exposes_readonly_pdf_subdirectory(self):
        resources = self.report_resources('--set', 'dailyReport.web.enabled=true')
        deployment = resources['Deployment', 'time']
        self.assertEqual(deployment['spec']['strategy'], {'type': 'Recreate'})
        pod = deployment['spec']['template']['spec']
        self.assertFalse(pod['automountServiceAccountToken'])
        web = pod['containers'][0]
        seed = pod['initContainers'][0]
        self.assertEqual(web['image'], IMAGE)
        self.assertEqual(seed['image'], IMAGE)
        self.assertEqual(seed['command'], ['python', '-m', 'report_cache'])
        self.assertNotIn('envFrom', web)
        self.assertNotIn('envFrom', seed)
        self.assertIn({'name': 'data', 'mountPath': '/data/reports', 'subPath': 'reports', 'readOnly': True}, web['volumeMounts'])
        self.assertTrue(web['securityContext']['readOnlyRootFilesystem'])
        self.assertFalse(any('secret' in volume for volume in pod['volumes']))
        cron_pod = resources['CronJob', 'daily-report']['spec']['jobTemplate']['spec']['template']['spec']
        affinity = cron_pod['affinity']['podAffinity']['requiredDuringSchedulingIgnoredDuringExecution'][0]
        self.assertEqual(affinity['topologyKey'], 'kubernetes.io/hostname')
        self.assertEqual(affinity['labelSelector']['matchLabels'], {'app.kubernetes.io/name': 'time'})
        route = resources['HTTPRoute', 'time']['spec']
        self.assertEqual(route['hostnames'], ['time.freddie.xyz'])
        self.assertEqual(route['rules'][0]['backendRefs'], [{'name': 'time', 'port': 8000}])
        self.assertEqual(resources['Certificate', 'time-tls']['spec']['dnsNames'], ['time.freddie.xyz'])
        self.assertIn('time.freddie.xyz', resources['ConfigMap', 'systems-dns-config']['data']['Corefile'])
        self.assertIn('time', resources['NetworkPolicy', 'gateway-to-applications']['spec']['podSelector']['matchExpressions'][0]['values'])

    def test_viewer_disabled_preserves_original_cronjob_scheduling(self):
        resources = self.report_resources()
        self.assertNotIn(('Deployment', 'time'), resources)
        pod = resources['CronJob', 'daily-report']['spec']['jobTemplate']['spec']['template']['spec']
        self.assertNotIn('affinity', pod)
