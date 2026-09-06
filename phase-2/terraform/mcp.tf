resource "konnect_gateway_service" "github_mcp" {
  control_plane_id = konnect_gateway_control_plane.phase2.id
  name             = "phase2-github-mcp"
  protocol         = "https"
  host             = "api.githubcopilot.com"
  port             = 443
  path             = "/mcp/readonly"
  retries          = 5
  connect_timeout  = 60000
  read_timeout     = 60000
  write_timeout    = 60000
  tags             = local.tags
}

resource "konnect_gateway_route" "github_mcp" {
  control_plane_id   = konnect_gateway_control_plane.phase2.id
  name               = "phase2-github-mcp"
  paths              = ["/github-mcp"]
  protocols          = ["http", "https"]
  strip_path         = true
  preserve_host      = false
  path_handling      = "v0"
  request_buffering  = true
  response_buffering = true
  tags               = local.tags

  service = {
    id = konnect_gateway_service.github_mcp.id
  }
}

resource "konnect_gateway_plugin_key_auth" "github_mcp" {
  control_plane_id = konnect_gateway_control_plane.phase2.id
  enabled          = true
  instance_name    = "phase2-github-mcp-key-auth"
  protocols        = ["http", "https"]
  tags             = local.tags

  route = {
    id = konnect_gateway_route.github_mcp.id
  }

  config = {
    key_names        = ["apikey"]
    key_in_header    = true
    key_in_query     = false
    key_in_body      = false
    hide_credentials = true
    run_on_preflight = true
  }
}

resource "konnect_gateway_plugin_ai_mcp_proxy" "github_mcp" {
  control_plane_id = konnect_gateway_control_plane.phase2.id
  enabled          = true
  instance_name    = "phase2-github-mcp-ai-mcp-proxy"
  protocols        = ["http", "https"]
  tags             = local.tags

  route = {
    id = konnect_gateway_route.github_mcp.id
  }

  config = {
    mode                    = "passthrough-listener"
    acl_attribute_type      = "consumer"
    consumer_identifier     = "username"
    include_consumer_groups = false
    max_request_body_size   = 8192

    default_acl = [
      {
        scope = "tools"
        allow = ["phase2-no-default-tool-access"]
      },
    ]

    logging = {
      log_audits     = true
      log_payloads   = false
      log_statistics = true
    }

    tools = [
      {
        name        = "get_file_contents"
        description = "Read one file from a GitHub repository"

        acl = {
          allow = ["phase2-developer"]
        }
      },
    ]
  }
}
