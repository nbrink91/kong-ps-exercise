variable "konnect_personal_access_token" {
  description = "Konnect Personal Access Token with permission to manage the Stage 2 control plane. Export as TF_VAR_konnect_personal_access_token."
  type        = string
  sensitive   = true
}

variable "konnect_server_url" {
  description = "Konnect API base URL for the organization region."
  type        = string
  default     = "https://us.api.konghq.com"
}

variable "control_plane_name" {
  description = "Name of the Terraform-managed Stage 2 control plane."
  type        = string
  default     = "ai-gateway-poc-phase2"
}

variable "anthropic_api_key" {
  description = "Anthropic API key stored in the Konnect Config Store. Export as TF_VAR_anthropic_api_key."
  type        = string
  sensitive   = true
}

variable "data_plane_client_certificate" {
  description = "PEM-encoded public certificate registered for the Stage 2 data plane. The private key never enters Terraform."
  type        = string
}

variable "openai_api_key" {
  description = "OpenAI API key stored as an Authorization header in the Konnect Config Store."
  type        = string
  sensitive   = true
}

variable "team_token_limits" {
  description = "Shared total-token allowances per team and provider for a sliding one-hour window."
  type        = map(object({ anthropic = number, openai = number }))
  default = {
    team-platform = { anthropic = 100000, openai = 100000 }
    team-apps     = { anthropic = 100000, openai = 100000 }
  }
  validation {
    condition = toset(keys(var.team_token_limits)) == toset(["team-platform", "team-apps"]) && alltrue(flatten([
      for limits in values(var.team_token_limits) : [
        for n in values(limits) : n > 0 && floor(n) == n
      ]
    ]))
    error_message = "Provide positive integer token limits for exactly team-platform and team-apps, with anthropic and openai allowances."
  }
}
