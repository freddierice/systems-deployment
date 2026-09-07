variable "kubeconfig_path" {
  type    = string
  default = "~/.kube/config"
}

variable "kube_context" {
  type    = string
  default = "do-nyc1-systems"
}

variable "tailscale_operator_version" {
  type    = string
  default = "1.102.3"
}

variable "apps" {
  description = "Enable only PostgreSQL-capable images after migration. Image must use an immutable sha256 digest."
  type = map(object({
    image = string
  }))
  default = {}
  validation {
    condition = alltrue([
      for name, app in var.apps : contains(["health", "trends"], name) && can(regex("@sha256:[0-9a-f]{64}$", app.image))
    ])
    error_message = "Only health and trends are supported; pin each image to its sha256 digest."
  }
}

variable "postgres_migration_verified" {
  description = "Explicit gate: both code compatibility and restored data have been verified before enabling apps."
  type        = bool
  default     = false
}

variable "image_pull_secrets" {
  description = "Existing Kubernetes registry pull Secret names in the systems namespace."
  type        = list(string)
  default     = []
}
