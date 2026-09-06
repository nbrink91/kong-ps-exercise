resource "konnect_gateway_service" "openai" {
  control_plane_id = konnect_gateway_control_plane.phase2.id
  name             = "phase2-openai"
  protocol         = "https"
  host             = "api.openai.com"
  port             = 443
  retries          = 5
  connect_timeout  = 60000
  read_timeout     = 60000
  write_timeout    = 60000
  tags             = local.tags
}

resource "konnect_gateway_route" "openai" {
  control_plane_id   = konnect_gateway_control_plane.phase2.id
  name               = "phase2-openai"
  paths              = ["/openai"]
  protocols          = ["http", "https"]
  strip_path         = true
  preserve_host      = false
  path_handling      = "v0"
  request_buffering  = true
  response_buffering = true
  tags               = local.tags
  service            = { id = konnect_gateway_service.openai.id }
}

resource "konnect_gateway_config_store_secret" "openai" {
  control_plane_id = konnect_gateway_control_plane.phase2.id
  config_store_id  = konnect_gateway_config_store.phase2_secrets.id
  key              = "openai-authorization"
  # A Vault reference replaces the whole header value, so store the Bearer prefix too.
  value = "Bearer ${var.openai_api_key}"
}

resource "konnect_gateway_plugin_key_auth" "openai" {
  control_plane_id = konnect_gateway_control_plane.phase2.id
  enabled          = true
  instance_name    = "phase2-openai-key-auth"
  protocols        = ["http", "https"]
  tags             = local.tags
  route            = { id = konnect_gateway_route.openai.id }
  config = {
    key_names   = ["apikey"], key_in_header = true, key_in_query = false,
    key_in_body = false, hide_credentials = true, run_on_preflight = true
  }
}

resource "konnect_gateway_plugin_ai_proxy" "openai" {
  control_plane_id = konnect_gateway_control_plane.phase2.id
  enabled          = true
  instance_name    = "phase2-openai-ai-proxy"
  protocols        = ["http", "https"]
  tags             = local.tags
  route            = { id = konnect_gateway_route.openai.id }
  config = {
    genai_category    = "text/generation", llm_format = "openai",
    route_type        = "llm/v1/responses", response_streaming = "allow",
    model_name_header = true, max_request_body_size = 524288
    auth = {
      allow_override = false
      header_name    = "Authorization"
      header_value   = "{vault://${konnect_gateway_vault.phase2_secrets.prefix}/${konnect_gateway_config_store_secret.openai.key}}"
    }
    logging = { log_payloads = false, log_statistics = true }
    model   = { name = "gpt-5.6-luna", provider = "openai" }
  }
}

resource "konnect_gateway_plugin_ai_sanitizer" "openai" {
  control_plane_id = konnect_gateway_control_plane.phase2.id
  enabled          = true
  instance_name    = "phase2-openai-ai-sanitizer"
  protocols        = ["http", "https"]
  tags             = local.tags
  route            = { id = konnect_gateway_route.openai.id }
  config           = konnect_gateway_plugin_ai_sanitizer.anthropic.config
}
