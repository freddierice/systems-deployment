output "cluster_id" {
  value = digitalocean_kubernetes_cluster.systems.id
}

output "cluster_name" {
  value = digitalocean_kubernetes_cluster.systems.name
}

output "kubeconfig_command" {
  value = "doctl kubernetes cluster kubeconfig save ${digitalocean_kubernetes_cluster.systems.id}"
}

output "database_id" {
  value = digitalocean_database_cluster.systems.id
}

output "database_private_host" {
  value = digitalocean_database_cluster.systems.private_host
}

# Consumed through a pipe by scripts/bootstrap-database.py, never a committed file.
output "database_bootstrap" {
  sensitive = true
  value = {
    host     = digitalocean_database_cluster.systems.private_host
    port     = digitalocean_database_cluster.systems.port
    ca       = data.digitalocean_database_ca.systems.certificate
    admin    = digitalocean_database_cluster.systems.user
    password = digitalocean_database_cluster.systems.password
    apps = {
      for name in local.apps : name => {
        database = digitalocean_database_db.app[name].name
        user     = digitalocean_database_user.app[name].name
        password = digitalocean_database_user.app[name].password
      }
    }
  }
}
