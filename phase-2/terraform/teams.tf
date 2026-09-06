locals {
  additional_developers = {
    platform_peer = { username = "phase2-developer-2", team = "team-platform" }
    apps          = { username = "phase2-app-developer", team = "team-apps" }
  }
  developer_teams = merge({
    developer = { id = konnect_gateway_consumer.developer.id, username = "phase2-developer", team = "team-platform" }
    }, {
    for key, developer in local.additional_developers : key => {
      id       = konnect_gateway_consumer.additional[key].id
      username = developer.username
      team     = developer.team
    }
  })
  llm_routes = {
    anthropic = konnect_gateway_route.anthropic.id
    openai    = konnect_gateway_route.openai.id
  }
  team_provider_limits = merge([
    for team, limits in var.team_token_limits : {
      for provider, limit in limits : "${team}-${provider}" => {
        team = team, provider = provider, limit = limit
      }
    }
  ]...)
}

resource "konnect_gateway_consumer" "developer" {
  control_plane_id = konnect_gateway_control_plane.phase2.id
  username         = "phase2-developer"
  tags             = local.tags
}

resource "konnect_gateway_key_auth" "developer" {
  control_plane_id = konnect_gateway_control_plane.phase2.id
  consumer_id      = konnect_gateway_consumer.developer.id
  tags             = local.tags
}

resource "konnect_gateway_consumer" "additional" {
  for_each         = local.additional_developers
  control_plane_id = konnect_gateway_control_plane.phase2.id
  username         = each.value.username
  tags             = local.tags
}

resource "konnect_gateway_key_auth" "additional" {
  for_each         = local.additional_developers
  control_plane_id = konnect_gateway_control_plane.phase2.id
  consumer_id      = konnect_gateway_consumer.additional[each.key].id
  tags             = local.tags
}

resource "konnect_gateway_consumer_group" "teams" {
  for_each         = var.team_token_limits
  control_plane_id = konnect_gateway_control_plane.phase2.id
  name             = each.key
  tags             = local.tags
}

resource "konnect_gateway_consumer_group_member" "developers" {
  for_each          = local.developer_teams
  control_plane_id  = konnect_gateway_control_plane.phase2.id
  consumer_group_id = konnect_gateway_consumer_group.teams[each.value.team].id
  consumer_id       = each.value.id
}

resource "konnect_gateway_plugin_acl" "llm_teams" {
  for_each         = local.llm_routes
  control_plane_id = konnect_gateway_control_plane.phase2.id
  enabled          = true
  instance_name    = "phase2-${each.key}-teams"
  protocols        = ["http", "https"]
  tags             = local.tags
  route            = { id = each.value }
  config = {
    allow              = sort(keys(var.team_token_limits)), include_consumer_groups = true,
    hide_groups_header = true, always_use_authenticated_groups = false
  }
}

resource "konnect_gateway_plugin_ai_rate_limiting_advanced" "teams" {
  for_each         = local.team_provider_limits
  control_plane_id = konnect_gateway_control_plane.phase2.id
  enabled          = true
  instance_name    = "phase2-${each.key}-token-limit"
  protocols        = ["http", "https"]
  tags             = local.tags
  route            = { id = local.llm_routes[each.value.provider] }
  consumer_group   = { id = konnect_gateway_consumer_group.teams[each.value.team].id }
  config = {
    identifier  = "consumer-group", strategy = "local", tokens_count_strategy = "total_tokens",
    window_type = "sliding", llm_format = each.value.provider, error_code = 429
    namespace   = "phase2-${each.key}"
    redis = {
      cluster_max_redirections = 5, connect_timeout = 2000,
      connection_is_proxied    = false, database = 0, host = "127.0.0.1",
      keepalive_pool_size      = 256, port = "6379", read_timeout = 2000,
      send_timeout             = 2000, ssl = false, ssl_verify = false
    }
    llm_providers = [{ name = each.value.provider, limit = [each.value.limit], window_size = [3600] }]
  }
}
