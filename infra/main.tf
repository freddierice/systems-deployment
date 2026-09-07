locals {
  apps = toset(["health", "trends"])
}

resource "digitalocean_vpc" "systems" {
  name        = "systems"
  region      = var.region
  description = "Private networking for systems DOKS and PostgreSQL"
  ip_range    = "10.70.0.0/20"
  lifecycle {
    prevent_destroy = true
  }
}

resource "digitalocean_vpc_nat_gateway" "systems" {
  name   = "systems-egress"
  type   = "PUBLIC"
  region = var.region
  size   = var.nat_size
  vpcs {
    vpc_uuid        = digitalocean_vpc.systems.id
    default_gateway = true
  }
}

data "digitalocean_kubernetes_versions" "systems" {
  version_prefix = var.kubernetes_version_prefix
}

# Read the allocated egress after creation; the resource's optional egress block
# is empty during its initial plan when DigitalOcean assigns the address.
data "digitalocean_vpc_nat_gateway" "systems" {
  id = digitalocean_vpc_nat_gateway.systems.id
}

resource "digitalocean_kubernetes_cluster" "systems" {
  name             = "systems"
  region           = var.region
  version          = data.digitalocean_kubernetes_versions.systems.latest_version
  vpc_uuid         = digitalocean_vpc.systems.id
  isolated_workers = true
  cluster_subnet   = "10.72.0.0/16"
  service_subnet   = "10.71.0.0/20"
  ha               = true
  auto_upgrade     = true
  surge_upgrade    = true

  control_plane_firewall {
    enabled = true
    # Isolated nodes bootstrap through NAT before their internal control-plane
    # route is established. Include this cluster's NAT egress explicitly.
    allowed_addresses = sort(distinct(concat(tolist(var.admin_cidrs), flatten([
      for egress in data.digitalocean_vpc_nat_gateway.systems.egresses : [
        for gateway in egress.public_gateways : "${gateway.ipv4}/32"
      ]
    ]))))
  }

  maintenance_policy {
    day        = "sunday"
    start_time = "06:00"
  }

  node_pool {
    name       = "systems-workers"
    size       = var.node_size
    node_count = var.node_count
    labels = {
      "systems.freddie.xyz/pool" = "applications"
    }
  }

  # Isolation is set at creation and needs a functioning default NAT gateway.
  depends_on = [digitalocean_vpc_nat_gateway.systems]
  lifecycle {
    prevent_destroy = true
  }
}

resource "digitalocean_database_cluster" "systems" {
  name                 = "systems-postgres"
  engine               = "pg"
  version              = var.postgres_version
  region               = var.region
  size                 = var.postgres_size
  node_count           = 1
  private_network_uuid = digitalocean_vpc.systems.id
  maintenance_window {
    day  = "sunday"
    hour = "07:00:00"
  }
  lifecycle {
    prevent_destroy = true
  }
}

resource "digitalocean_database_firewall" "systems" {
  cluster_id = digitalocean_database_cluster.systems.id
  rule {
    type  = "k8s"
    value = digitalocean_kubernetes_cluster.systems.id
  }
}

resource "digitalocean_database_db" "app" {
  for_each   = local.apps
  cluster_id = digitalocean_database_cluster.systems.id
  name       = each.key
  lifecycle {
    prevent_destroy = true
  }
}

resource "digitalocean_database_user" "app" {
  for_each   = local.apps
  cluster_id = digitalocean_database_cluster.systems.id
  name       = each.key
  # The API returns an empty settings block for PostgreSQL users.
  settings {}
}

data "digitalocean_database_ca" "systems" {
  cluster_id = digitalocean_database_cluster.systems.id
}
