variable "region" {
  description = "Region shared by the VPC, DOKS, NAT gateway, and PostgreSQL."
  type        = string
  default     = "nyc1"
}

variable "admin_cidrs" {
  description = "Public egress CIDRs for the machines running Terraform/kubectl. Required for bootstrap; Tailscale 100.x addresses do not work here."
  type        = set(string)
  validation {
    condition = length(var.admin_cidrs) > 0 && alltrue([
      for address in var.admin_cidrs : can(cidrhost(address, 0)) && try(tonumber(split("/", address)[1]) > 0, false)
    ])
    error_message = "Provide at least one valid, non-world-open administrator CIDR."
  }
}

variable "kubernetes_version_prefix" {
  description = "Resolve the newest patch in this Kubernetes minor. Isolated workers require 1.36+."
  type        = string
  default     = "1.36."
  validation {
    condition     = can(regex("^1\\.(3[6-9]|[4-9][0-9])\\.$", var.kubernetes_version_prefix))
    error_message = "Use a Kubernetes minor prefix of 1.36. or later."
  }
}

variable "node_size" {
  type    = string
  default = "s-2vcpu-4gb"
}

variable "node_count" {
  type    = number
  default = 2
  validation {
    condition     = var.node_count >= 1 && floor(var.node_count) == var.node_count
    error_message = "node_count must be a positive integer."
  }
}

variable "postgres_version" {
  type    = string
  default = "17"
}

variable "postgres_size" {
  description = "One managed primary, shared by both apps. This default has no standby."
  type        = string
  default     = "db-s-1vcpu-2gb"
}

variable "nat_size" {
  type    = number
  default = 1
  validation {
    condition     = var.nat_size >= 1 && floor(var.nat_size) == var.nat_size
    error_message = "nat_size must be a positive integer."
  }
}
