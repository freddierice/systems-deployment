resource "helm_release" "tailscale" {
  name             = "tailscale-operator"
  repository       = "https://pkgs.tailscale.com/helmcharts"
  chart            = "tailscale-operator"
  version          = var.tailscale_operator_version
  namespace        = "tailscale"
  create_namespace = true
  atomic           = true
  timeout          = 600

  # Pre-create tailscale/operator-oauth outside Terraform; no client secret in
  # Helm values or Terraform state. See scripts/bootstrap-operator.sh.
  values = [yamlencode({
    operatorConfig = {
      hostname    = "systems-operator"
      defaultTags = ["tag:systems-operator"]
      resources = {
        requests = { cpu = "50m", memory = "128Mi" }
        limits   = { memory = "256Mi" }
      }
    }
    proxyConfig          = { defaultTags = "tag:systems" }
    apiServerProxyConfig = { mode = "noauth" }
  })]
}

resource "helm_release" "systems" {
  name             = "systems"
  chart            = "${path.module}/../charts/systems"
  namespace        = "systems"
  create_namespace = true
  atomic           = true
  timeout          = 600

  values = [yamlencode({
    postgresMigrationVerified = var.postgres_migration_verified
    imagePullSecrets          = [for name in var.image_pull_secrets : { name = name }]
    apps = {
      for name in ["health", "trends"] : name => {
        enabled = contains(keys(var.apps), name)
        image   = try(var.apps[name].image, "")
      }
    }
  })]

  # Hash local chart sources to cause upgrades when templates change.
  set = [{
    name  = "chartRevision"
    value = sha256(join("", [for chart_file in sort(fileset("${path.module}/../charts/systems", "**")) : filesha256("${path.module}/../charts/systems/${chart_file}")]))
  }]

  depends_on = [helm_release.tailscale]
  lifecycle {
    precondition {
      condition     = length(var.apps) == 0 || var.postgres_migration_verified
      error_message = "Apps still need PostgreSQL support and a verified data migration. Keep apps empty until that work is complete."
    }
  }
}
