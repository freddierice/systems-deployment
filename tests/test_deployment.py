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
        self.assertFalse(any(r["kind"] == "PersistentVolumeClaim" for r in resources))
        config = next(r["data"] for r in resources if r["metadata"]["name"] == "systems-dns-config")
        self.assertEqual(set(config), {"Corefile"})
        self.assertIn("ca-tailnet.systems.svc.cluster.local", config["Corefile"])
        self.assertIn("systems-gateway.systems.svc.cluster.local", config["Corefile"])
        gateway = next(r for r in resources if r["kind"] == "Gateway")
        certificates = {r["spec"]["secretName"]: r for r in resources if r["kind"] == "Certificate"}
        for listener in gateway["spec"]["listeners"]:
            if listener["protocol"] == "HTTPS":
                cert = certificates[listener["tls"]["certificateRefs"][0]["name"]]
                self.assertEqual(cert["spec"]["dnsNames"], [listener["hostname"]])
                self.assertEqual(cert["spec"]["renewBefore"], "8h")
        issuer = next(r for r in resources if r["kind"] == "Issuer")
        solver = issuer["spec"]["acme"]["solvers"][0]["http01"]["gatewayHTTPRoute"]
        self.assertEqual(solver["parentRefs"][0]["sectionName"], "http")
        self.assertEqual(solver["serviceType"], "ClusterIP")
        self.assertTrue(issuer["spec"]["acme"]["caBundle"])
        traefik = yaml.safe_load((ROOT / "kubernetes/traefik.values.yaml").read_text())
        # Cross-chart contract: incorrect named target ports silently leave the
        # Tailscale Service without HTTPS endpoints, despite healthy pods.
        ports = traefik["ports"]
        for port in lbs[0]["spec"]["ports"]:
            self.assertEqual(ports[port["targetPort"]]["port"], port["port"])
        self.assertEqual(lbs[0]["spec"]["selector"], traefik["deployment"]["podLabels"])
        self.assertFalse(traefik["service"]["enabled"])
        for route in (r for r in resources if r["kind"] == "HTTPRoute"):
            self.assertEqual({m["method"] for m in route["spec"]["rules"][0]["matches"]}, {"GET", "HEAD"})
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
        routes = {r["metadata"]["name"]: r for r in resources if r["kind"] == "HTTPRoute"}
        for name in ("health", "trends"):
            self.assertEqual(routes[name]["spec"]["rules"][0]["backendRefs"], [{"name": name, "port": 8000}])
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
            policy = next(r for r in resources if r["metadata"]["name"] == "gateway-to-applications")
            self.assertEqual(policy["spec"]["ingress"][0]["from"], [{"podSelector": {"matchLabels": {"systems.freddie.xyz/gateway": "true"}}}])


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


class RuntimeIdentityTests(unittest.TestCase):
    def test_production_uses_docr_and_short_lived_google_identity(self):
        root = Path(__file__).resolve().parents[1]
        rendered = subprocess.check_output([
            'helm', 'template', 'systems', str(root / 'kubernetes/charts/systems'),
            '--namespace', 'systems', '-f', str(root / 'kubernetes/values.production.yaml')
        ], text=True)
        documents = list(yaml.safe_load_all(rendered))
        apps = {d['metadata']['name']: d for d in documents if d and d['kind'] == 'Deployment' and d['metadata']['name'] in ('health', 'trends')}
        for name, deployment in apps.items():
            pod = deployment['spec']['template']['spec']
            self.assertEqual(pod['imagePullSecrets'], [{'name': 'freddierice-systems'}])
            self.assertFalse(pod['automountServiceAccountToken'])
            self.assertEqual(pod['serviceAccountName'], name)
            self.assertTrue(pod['containers'][0]['image'].startswith('registry.digitalocean.com/freddierice-systems/' + name + '@sha256:'))
        pod = apps['trends']['spec']['template']['spec']
        volumes = {v['name']: v for v in pod['volumes']}
        token = volumes['google-token']['projected']['sources'][0]['serviceAccountToken']
        self.assertEqual(token['expirationSeconds'], 3600)
        self.assertIn('workloadIdentityPools/systems/providers/doks', token['audience'])
        self.assertNotIn('secret', volumes['google-config'])


if __name__ == "__main__":
    unittest.main()
