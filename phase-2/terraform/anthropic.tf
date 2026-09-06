resource "konnect_gateway_service" "anthropic" {
  control_plane_id = konnect_gateway_control_plane.phase2.id
  name             = "phase2-anthropic"
  protocol         = "https"
  host             = "api.anthropic.com"
  port             = 443
  retries          = 5
  connect_timeout  = 60000
  read_timeout     = 60000
  write_timeout    = 60000
  tags             = local.tags
}

resource "konnect_gateway_route" "anthropic" {
  control_plane_id   = konnect_gateway_control_plane.phase2.id
  name               = "phase2-anthropic"
  paths              = ["/anthropic"]
  protocols          = ["http", "https"]
  strip_path         = true
  preserve_host      = false
  path_handling      = "v0"
  request_buffering  = true
  response_buffering = true
  tags               = local.tags

  service = {
    id = konnect_gateway_service.anthropic.id
  }
}

resource "konnect_gateway_config_store_secret" "anthropic" {
  control_plane_id = konnect_gateway_control_plane.phase2.id
  config_store_id  = konnect_gateway_config_store.phase2_secrets.id
  key              = "anthropic-api-key"
  value            = var.anthropic_api_key
}

resource "konnect_gateway_plugin_key_auth" "anthropic" {
  control_plane_id = konnect_gateway_control_plane.phase2.id
  enabled          = true
  instance_name    = "phase2-anthropic-key-auth"
  protocols        = ["http", "https"]
  tags             = local.tags

  route = {
    id = konnect_gateway_route.anthropic.id
  }

  config = {
    key_names        = ["x-api-key"]
    key_in_header    = true
    key_in_query     = false
    key_in_body      = false
    hide_credentials = true
    run_on_preflight = true
  }
}

resource "konnect_gateway_plugin_ai_proxy" "anthropic" {
  control_plane_id = konnect_gateway_control_plane.phase2.id
  enabled          = true
  instance_name    = "phase2-anthropic-ai-proxy"
  protocols        = ["http", "https"]
  tags             = local.tags

  route = {
    id = konnect_gateway_route.anthropic.id
  }

  config = {
    genai_category        = "text/generation"
    llm_format            = "anthropic"
    route_type            = "llm/v1/chat"
    response_streaming    = "allow"
    model_name_header     = true
    max_request_body_size = 524288

    auth = {
      allow_override = false
      header_name    = "x-api-key"
      header_value = format(
        "{vault://%s/%s}",
        konnect_gateway_vault.phase2_secrets.prefix,
        konnect_gateway_config_store_secret.anthropic.key,
      )
    }

    logging = {
      log_payloads   = false
      log_statistics = true
    }

    model = {
      name     = "claude-sonnet-5"
      provider = "anthropic"

      options = {
        anthropic_version = "2023-06-01"
      }
    }
  }
}

resource "konnect_gateway_plugin_ai_sanitizer" "anthropic" {
  control_plane_id = konnect_gateway_control_plane.phase2.id
  enabled          = true
  instance_name    = "phase2-anthropic-ai-sanitizer"
  protocols        = ["http", "https"]
  tags             = local.tags

  route = {
    id = konnect_gateway_route.anthropic.id
  }

  config = {
    anonymize                      = ["all_and_credentials"]
    allow_all_conversation_history = true
    block_if_detected              = false
    host                           = "phase2-pii.kong-stage2.svc.cluster.local"
    keepalive_timeout              = 60000
    port                           = 8080
    recover_redacted               = false
    redact_type                    = "placeholder"
    sanitization_mode              = "INPUT"
    scheme                         = "http"
    skip_logging_sanitized_items   = true
    stop_on_error                  = true
    timeout                        = 10000
  }
}
