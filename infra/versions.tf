terraform {
  required_version = ">= 1.11, < 2.0"
  required_providers {
    digitalocean = {
      source  = "digitalocean/digitalocean"
      version = "~> 2.100.0"
    }
  }
}

# Set DIGITALOCEAN_TOKEN in the environment.
provider "digitalocean" {}
