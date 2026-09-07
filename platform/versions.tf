terraform {
  required_version = ">= 1.11, < 2.0"
  required_providers {
    helm = {
      source  = "hashicorp/helm"
      version = "~> 3.3.0"
    }
  }
}

# The infra root must be applied and kubeconfig saved first. Keep cloud creation
# separate from Kubernetes provider initialization and CRD installation.
provider "helm" {
  kubernetes = {
    config_path    = pathexpand(var.kubeconfig_path)
    config_context = var.kube_context
  }
}
