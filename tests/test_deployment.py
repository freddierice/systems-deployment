"""Render Helm and check exposure, CA routing, and app migration boundaries."""
import importlib.util
from pathlib import Path
import subprocess
import unittest
from urllib.parse import parse_qs, unquote, urlsplit

import yaml

ROOT = Path(__file__).resolve().parents[1]


def render(*arguments):
    return subprocess.run(
        ["helm", "template", "systems", str(ROOT / "kubernetes/charts/systems"), "--namespace", "systems", *arguments],
        text=True, capture_output=True,
    )


class ChartTests(unittest.TestCase):
    def test_preparation_has_one_private_load_balancer_and_no_apps(self):
        result = render()
        self.assertEqual(result.returncode, 0, result.stderr)
        resources = list(yaml.safe_load_all(result.stdout))
        services = [r for r in resources if r["kind"] == "Service"]
        lbs = [r for r in services if r["spec"].get("type") == "LoadBalancer"]
        self.assertEqual(len(lbs), 1)
        self.assertEqual(lbs[0]["spec"]["loadBalancerClass"], "tailscale")
        self.assertIs(lbs[0]["spec"]["allocateLoadBalancerNodePorts"], False)
        self.assertEqual({p["port"] for p in lbs[0]["spec"]["ports"]}, {80, 443})
        self.assertFalse(any(r["metadata"]["name"] in ("health", "trends") for r in resources))
        self.assertFalse(any(r["spec"].get("type") == "NodePort" for r in services))
        config = next(r["data"] for r in resources if r["metadata"]["name"] == "systems-config")
        self.assertIn("https://ca.freddie.xyz/acme/acme/directory", config["Caddyfile"])
        self.assertIn("disable_tlsalpn_challenge", config["Caddyfile"])
        self.assertIn("ca-tailnet.systems.svc.cluster.local", config["Corefile"])
        self.assertIn("BEGIN CERTIFICATE", config["root_ca.crt"])
        self.assertNotIn("PRIVATE KEY", config["root_ca.crt"])
        for resource in resources:
            if resource["kind"] == "Deployment":
                pod = resource["spec"]["template"]["spec"]
                self.assertIs(pod["automountServiceAccountToken"], False)
                self.assertFalse(pod.get("hostNetwork", False))

    def test_enabling_unmigrated_app_fails(self):
        result = render("--set", "apps.health.enabled=true")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("postgresMigrationVerified=true", result.stderr)

    def test_mutable_app_image_fails(self):
        result = render("--set", "apps.health.enabled=true", "--set", "postgresMigrationVerified=true", "--set", "apps.health.image=health:latest")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("immutable image digest", result.stderr)

    def test_app_contract_uses_secrets_private_services_and_real_probes(self):
        args = ["--set", "postgresMigrationVerified=true"]
        for name in ("health", "trends"):
            args += ["--set", f"apps.{name}.enabled=true", "--set", f"apps.{name}.image=ghcr.io/freddierice/{name}@sha256:" + "a" * 64]
        result = render(*args)
        self.assertEqual(result.returncode, 0, result.stderr)
        resources = list(yaml.safe_load_all(result.stdout))
        for name in ("health", "trends"):
            svc = next(r for r in resources if r["kind"] == "Service" and r["metadata"]["name"] == name)
            self.assertEqual(svc["spec"]["type"], "ClusterIP")
            deploy = next(r for r in resources if r["kind"] == "Deployment" and r["metadata"]["name"] == name)
            self.assertEqual(deploy["spec"]["replicas"], 1)
            container = deploy["spec"]["template"]["spec"]["containers"][0]
            env = {e["name"]: e for e in container["env"]}
            self.assertEqual(env["DATABASE_URL"]["valueFrom"]["secretKeyRef"]["name"], f"{name}-database")
            self.assertEqual(env["PGSSLMODE"]["value"], "verify-full")
            self.assertEqual(container["readinessProbe"]["exec"]["command"][-1], "/ready")
            self.assertEqual(container["livenessProbe"]["exec"]["command"][-1], "/health")
            self.assertIs(container["securityContext"]["readOnlyRootFilesystem"], True)
            policy = next(r for r in resources if r["metadata"]["name"] == f"gateway-to-{name}")
            self.assertEqual(policy["spec"]["ingress"][0]["from"], [{"podSelector": {"matchLabels": {"app.kubernetes.io/name": "systems-gateway"}}}])


class BootstrapTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        spec = importlib.util.spec_from_file_location("bootstrap", ROOT / "scripts/bootstrap-database.py")
        cls.bootstrap = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(cls.bootstrap)

    def test_database_password_is_url_encoded_and_tls_verifies_identity(self):
        password = "space @:/?#&+%"
        url = self.bootstrap.database_url(
            {"host": "private-example.db.ondigitalocean.com", "port": 25060},
            {"database": "health", "user": "health", "password": password},
        )
        parsed = urlsplit(url)
        self.assertEqual(unquote(parsed.password), password)
        self.assertEqual(parsed.hostname, "private-example.db.ondigitalocean.com")
        self.assertEqual(parse_qs(parsed.query)["sslmode"], ["verify-full"])
        self.assertEqual(parse_qs(parsed.query)["sslrootcert"], ["/etc/postgresql/ca.crt"])

    def test_preflight_python_compiles(self):
        source = (ROOT / "scripts/preflight.sh").read_text()
        for snippet in source.split("python3 -c '\n")[1:]:
            compile(snippet.split("\n'", 1)[0], "preflight", "exec")

    def test_grants_script_is_valid_shell(self):
        result = subprocess.run(["sh", "-n"], input=self.bootstrap.grant_script(), text=True, capture_output=True)
        self.assertEqual(result.returncode, 0, result.stderr)


if __name__ == "__main__":
    unittest.main()
