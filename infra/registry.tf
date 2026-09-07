resource "digitalocean_container_registry" "systems" {
  name                   = "freddierice-systems"
  subscription_tier_slug = "basic"
  region                 = "nyc3"
  lifecycle {
    prevent_destroy = true
  }
}

output "registry_endpoint" {
  value = digitalocean_container_registry.systems.endpoint
}
