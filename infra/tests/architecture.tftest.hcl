mock_provider "digitalocean" {}

variables {
  admin_cidrs = ["203.0.113.10/32"]
}

run "private_cluster_and_shared_database" {
  command = plan
  assert {
    condition     = digitalocean_kubernetes_cluster.systems.name == "systems" && digitalocean_kubernetes_cluster.systems.isolated_workers
    error_message = "systems must use isolated workers."
  }
  assert {
    condition     = digitalocean_kubernetes_cluster.systems.control_plane_firewall[0].enabled && length(digitalocean_kubernetes_cluster.systems.control_plane_firewall[0].allowed_addresses) == 1
    error_message = "The API server needs a restrictive control-plane firewall."
  }
  assert {
    condition     = one(digitalocean_vpc_nat_gateway.systems.vpcs).default_gateway
    error_message = "Isolated workers require a default VPC NAT gateway."
  }
  assert {
    condition     = digitalocean_database_cluster.systems.engine == "pg" && digitalocean_database_cluster.systems.node_count == 1
    error_message = "Use one managed PostgreSQL primary."
  }
  assert {
    condition     = toset(keys(digitalocean_database_db.app)) == toset(["health", "trends"]) && toset(keys(digitalocean_database_user.app)) == toset(["health", "trends"])
    error_message = "Each app needs its own logical database and login."
  }
  assert {
    condition     = one(digitalocean_database_firewall.systems.rule).type == "k8s"
    error_message = "The database trusted source must be the Kubernetes cluster."
  }
}

run "reject_world_open_admin" {
  command = plan
  variables {
    admin_cidrs = ["0.0.0.0/0"]
  }
  expect_failures = [var.admin_cidrs]
}

run "reject_missing_admin" {
  command = plan
  variables {
    admin_cidrs = []
  }
  expect_failures = [var.admin_cidrs]
}

run "reject_old_kubernetes" {
  command = plan
  variables {
    kubernetes_version_prefix = "1.35."
  }
  expect_failures = [var.kubernetes_version_prefix]
}
