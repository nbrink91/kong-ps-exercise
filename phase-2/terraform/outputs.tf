output "control_plane_id" {
  description = "Terraform-managed Stage 2 control plane UUID."
  value       = konnect_gateway_control_plane.phase2.id
}

output "cluster_control_plane" {
  description = "Normalized host:443 endpoint for KONG_CLUSTER_CONTROL_PLANE."
  value       = "${local.control_plane_host}:443"
}

output "cluster_server_name" {
  description = "Host-only SNI for KONG_CLUSTER_SERVER_NAME."
  value       = local.control_plane_host
}

output "cluster_telemetry_endpoint" {
  description = "Normalized host:443 endpoint for KONG_CLUSTER_TELEMETRY_ENDPOINT."
  value       = "${local.telemetry_host}:443"
}

output "cluster_telemetry_server_name" {
  description = "Host-only SNI for KONG_CLUSTER_TELEMETRY_SERVER_NAME."
  value       = local.telemetry_host
}

output "consumer_key" {
  description = "Generated Key Auth credential for phase2-developer."
  value       = konnect_gateway_key_auth.developer.key
  sensitive   = true
}

output "consumer_keys" {
  description = "Per-developer generated credentials. Keep private and do not commit."
  sensitive   = true
  value = merge({ "phase2-developer" = konnect_gateway_key_auth.developer.key }, {
    for key, developer in local.additional_developers : developer.username => konnect_gateway_key_auth.additional[key].key
  })
}

output "consumer_teams" {
  description = "Nonsecret Consumer identity and team mapping for usage attribution."
  value       = local.developer_teams
}
