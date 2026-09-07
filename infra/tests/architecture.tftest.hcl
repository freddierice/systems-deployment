mock_provider "digitalocean" {
  override_during = plan
}

override_data {
  target          = data.digitalocean_vpc_nat_gateway.systems
  override_during = plan
  values = {
    egresses = [{ public_gateways = [{ ipv4 = "198.51.100.10" }] }]
  }
}

variables {
  admin_cidrs = ["203.0.113.10/32"]
}

run "private_cluster_and_shared_database" {
  # Exercise the allocated NAT address after a mocked creation/read cycle.
  command = apply
  assert {
    condition     = digitalocean_kubernetes_cluster.systems.name == "systems" && digitalocean_kubernetes_cluster.systems.isolated_workers
    error_message = "systems must use isolated workers."
  }
  assert {
    condition     = digitalocean_kubernetes_cluster.systems.control_plane_firewall[0].enabled && toset(digitalocean_kubernetes_cluster.systems.control_plane_firewall[0].allowed_addresses) == toset(["203.0.113.10/32", "198.51.100.10/32"])
    error_message = "The API server must allow only administrator CIDRs and the cluster NAT egress."
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
