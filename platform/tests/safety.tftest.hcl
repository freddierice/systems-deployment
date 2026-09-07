mock_provider "helm" {}

run "preparation_only" {
  command = plan
  assert {
    condition     = !var.postgres_migration_verified && length(var.apps) == 0
    error_message = "Applications must be disabled before PostgreSQL migration."
  }
}

run "reject_unmigrated_app" {
  command = plan
  variables {
    apps = {
      health = { image = "ghcr.io/freddierice/health@sha256:aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa" }
    }
  }
  expect_failures = [helm_release.systems]
}

run "reject_mutable_image" {
  command = plan
  variables {
    postgres_migration_verified = true
    apps                        = { health = { image = "ghcr.io/freddierice/health:latest" } }
  }
  expect_failures = [var.apps]
}
