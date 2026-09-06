locals {
  tags = [
    "managed-by:terraform",
    "stage:phase2",
  ]

  control_plane_host = trimsuffix(
    trimprefix(konnect_gateway_control_plane.phase2.config.control_plane_endpoint, "https://"),
    ":443",
  )
  telemetry_host = trimsuffix(
    trimprefix(konnect_gateway_control_plane.phase2.config.telemetry_endpoint, "https://"),
    ":443",
  )
}

resource "konnect_gateway_control_plane" "phase2" {
  name         = var.control_plane_name
  description  = "AI Gateway POC Phase 2 hybrid Kubernetes data plane"
  cluster_type = "CLUSTER_TYPE_CONTROL_PLANE"
  auth_type    = "pinned_client_certs"
  labels = {
    managed-by = "terraform"
    stage      = "phase2"
  }
}

resource "konnect_gateway_data_plane_client_certificate" "phase2" {
  title            = "AI Gateway POC Phase 2 Docker Desktop"
  control_plane_id = konnect_gateway_control_plane.phase2.id
  cert             = var.data_plane_client_certificate
}

resource "konnect_gateway_config_store" "phase2_secrets" {
  control_plane_id = konnect_gateway_control_plane.phase2.id
  name             = "phase2-secrets"
}

resource "konnect_gateway_vault" "phase2_secrets" {
  control_plane_id = konnect_gateway_control_plane.phase2.id
  name             = "konnect"
  prefix           = "phase2-secrets"
  description      = "Stage 2 Konnect Config Store"
  tags             = local.tags

  config = jsonencode({
    config_store_id = konnect_gateway_config_store.phase2_secrets.id
  })
}
