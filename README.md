# Kong AI Gateway POC

Route Claude Code, Codex, and an approved GitHub read through Kong. Each developer
has a Consumer key and one team membership. Teams share 100,000 tokens per hour
for each provider. [WRITEUP.md](WRITEUP.md) covers decisions and results.

[VALIDATION.md](VALIDATION.md) provides repeatable client and policy checks and
recovery steps. See the [results](RESULTS.md) and the
[redaction limitations](RESULTS.md#redaction-limitations) before
using the POC with sensitive data.

| Route | Client and upstream | Gateway authentication |
| --- | --- | --- |
| `/anthropic` | Claude Code → Anthropic `claude-sonnet-5` | Consumer key in `x-api-key` |
| `/openai` | Codex → OpenAI `gpt-5.6-luna` | Consumer key in `apikey` |
| `/github-mcp` | GitHub MCP, `get_file_contents` only | Consumer key in `apikey`, GitHub bearer token |

## 1. Prepare credentials

Install Docker Desktop, Terraform 1.5+, Helm, kubectl, OpenSSL, Claude Code, and
Codex. Enable Docker Desktop Kubernetes for Phase 2. The POC pins Gateway 3.15.0.5
and Kong chart 3.4.1; Docker Desktop used 16 GB of memory.
Client validation used Claude Code 2.1.260 and Codex 0.153.0. Minimum client
versions have not been established.

You need a Konnect organization with AI Gateway Enterprise entitlement or an
enabled trial that includes the required AI plugins:
[AI MCP Proxy](https://developer.konghq.com/plugins/ai-mcp-proxy/),
[AI PII Sanitizer](https://developer.konghq.com/plugins/ai-sanitizer/), and
[AI Rate Limiting Advanced](https://developer.konghq.com/plugins/ai-rate-limiting-advanced/).
You also need API access to both models and a GitHub token with repository read
access. From the repository root, create `.env` if it does not exist, fill in its
blank credentials, and load it:

```sh
test -f .env || (umask 077; cp .env.example .env)
chmod 600 .env
set -a
source .env
set +a
```

These instructions use Konnect's US region (`https://us.api.konghq.com`). For
another region, set `TF_VAR_konnect_server_url` in `.env` to its Konnect API base
URL and reload the file before setup. Select the same region in Konnect when
creating Phase 1; Terraform and both validation phases use this API URL.

Run all commands from this directory. Exclude `.env` and `.local/` when sharing a
ZIP. Both are ignored by Git. `.local/` holds generated client files, certificates,
and Terraform state, which contains credentials.

## 2. Configure Phase 1 in Konnect

Create a self-managed control plane named `ai-gateway-poc-phase1`. Generate its
data-plane certificate in Konnect and save the pair as `.local/phase-1/cluster.crt`
and `.local/phase-1/cluster.key`. Create the directory first:

```sh
install -d -m 700 .local/phase-1
```

Use the control-plane and telemetry hostnames without `https://` or `:443`, then
start Docker Compose:

```sh
export KONNECT_CONTROL_PLANE_HOST='CONTROL_PLANE_HOSTNAME'
export KONNECT_TELEMETRY_HOST='TELEMETRY_HOSTNAME'
chmod 600 .local/phase-1/cluster.key
export KONG_CLUSTER_CERT="$(cat .local/phase-1/cluster.crt)"
export KONG_CLUSTER_CERT_KEY="$(cat .local/phase-1/cluster.key)"
docker compose -f phase-1/compose.yaml up -d
```

Confirm the node is **Connected, In sync, and Compatible** in Konnect.
Optionally set `KONNECT_PHASE1_CONTROL_PLANE_ID` in `.env` to select this control
plane by ID during validation; otherwise validation uses the name above.

### Services, identities, and secrets

Create these HTTPS Services on port 443. Give each Route the same name as its
Service, accept HTTP/HTTPS, and enable **Strip path**.

| Service and Route | Service host | Service path | Route path |
| --- | --- | --- | --- |
| `phase1-anthropic` | `api.anthropic.com` | Empty | `/anthropic` |
| `phase1-openai` | `api.openai.com` | Empty | `/openai` |
| `phase1-github-mcp` | `api.githubcopilot.com` | `/mcp/readonly` | `/github-mcp` |

Create Consumer Groups `team-platform` and `team-apps`. Add `phase1-developer` and
`phase1-developer-2` to platform, and `phase1-app-developer` to apps. Give each
Consumer a distinct Key Auth credential. Save the original developer's key as
`KONG_CONSUMER_KEY` in `.env` and reload the file.

Create Config Store `phase1-secrets` with keys `anthropic-api-key` and
`openai-authorization`. Their values are the Anthropic key and
`Bearer <OPENAI_API_KEY>`, respectively. Create a Vault with provider `konnect`,
prefix `phase1-secrets`, and `config_store_id` set to this store's ID.

### Route policies

Add Key Auth to all Routes: `key_names=[x-api-key]` for Anthropic and
`key_names=[apikey]` for OpenAI and MCP. Set `key_in_header=true`,
`key_in_query=false`, `key_in_body=false`, and `hide_credentials=true`.

Add AI Proxy to each model Route:

| Setting | Anthropic | OpenAI |
| --- | --- | --- |
| `model.provider` / `model.name` | `anthropic` / `claude-sonnet-5` | `openai` / `gpt-5.6-luna` |
| `llm_format` / `route_type` | `anthropic` / `llm/v1/chat` | `openai` / `llm/v1/responses` |
| `auth.header_name` | `x-api-key` | `Authorization` |
| `auth.header_value` | `{vault://phase1-secrets/anthropic-api-key}` | `{vault://phase1-secrets/openai-authorization}` |
| `model.options.anthropic_version` | `2023-06-01` | Omit |

For both, set `response_streaming=allow`, `auth.allow_override=false`,
`logging.log_payloads=false`, and `logging.log_statistics=true`.

- **ACL:** On each model Route, allow `team-platform` and `team-apps`. Set
  `include_consumer_groups=true`, `hide_groups_header=true`, and
  `always_use_authenticated_groups=false`.
- **AI Rate Limiting Advanced:** Create one instance for each team/Route pair,
  selecting both the Consumer Group and Route. Set `identifier=consumer-group`,
  `strategy=local`, `tokens_count_strategy=total_tokens`, `window_type=sliding`,
  `error_code=429`, and `llm_format` to the provider. In `llm_providers`, add that
  provider's `name`, `limit=[100000]`, and `window_size=[3600]`. Use a distinct
  namespace for each: `phase1-team-platform-anthropic`, `phase1-team-platform-openai`,
  `phase1-team-apps-anthropic`, `phase1-team-apps-openai`.
- **AI Sanitizer:** On both model Routes, set `scheme=http`,
  `host=host.docker.internal`, `port=8080`, `anonymize=[all_and_credentials]`,
  `sanitization_mode=INPUT`, `redact_type=placeholder`,
  `allow_all_conversation_history=true`, `recover_redacted=false`,
  `block_if_detected=false`, `stop_on_error=true`,
  `skip_logging_sanitized_items=true`, `timeout=10000`, and `keepalive_timeout=60000`.
- **AI MCP Proxy:** On the MCP Route only, set `mode=passthrough-listener`,
  `acl_attribute_type=consumer`, `consumer_identifier=username`, and
  `include_consumer_groups=false`. Add a `default_acl` entry with `scope=tools`
  and `allow=[phase1-no-default-tool-access]`; do not create that Consumer.
  Add a `tools` entry with `name=get_file_contents`,
  `description="Read one file from a GitHub repository"`, and
  `acl.allow=[phase1-developer]`. Set `logging.log_audits=true`,
  `logging.log_statistics=true`, and `logging.log_payloads=false`.
  Pass the GitHub Authorization header upstream unchanged.

## 3. Deploy Phase 2

Use the `docker-desktop` Kubernetes context. The following reuses an existing
certificate pair. Restore a missing file from an existing pair before continuing.

```sh
export TF_DATA_DIR="$PWD/.local/phase-2/terraform-data"
install -d -m 700 .local/phase-2 "$TF_DATA_DIR"
test -e .local/phase-2/cluster.crt || test -e .local/phase-2/cluster.key || \
  openssl req -new -x509 -nodes -newkey rsa:2048 \
    -keyout .local/phase-2/cluster.key -out .local/phase-2/cluster.crt \
    -subj '/CN=ai-gateway-poc-phase2-docker-desktop' -days 825
chmod 600 .local/phase-2/cluster.key
export TF_VAR_data_plane_client_certificate="$(cat .local/phase-2/cluster.crt)"
terraform -chdir=phase-2/terraform init
terraform -chdir=phase-2/terraform validate
terraform -chdir=phase-2/terraform plan -out="$PWD/.local/phase-2/deploy.tfplan"
```

Review the plan, then apply it and deploy the runtime:

```sh
terraform -chdir=phase-2/terraform apply "$PWD/.local/phase-2/deploy.tfplan"
kubectl --context docker-desktop create namespace kong-stage2 --dry-run=client -o yaml \
  | kubectl --context docker-desktop apply -f -
kubectl --context docker-desktop -n kong-stage2 create secret generic kong-cluster-cert \
  --from-file=tls.crt=.local/phase-2/cluster.crt \
  --from-file=tls.key=.local/phase-2/cluster.key --dry-run=client -o yaml \
  | kubectl --context docker-desktop apply -f -
kubectl --context docker-desktop apply -f phase-2/kubernetes/pii.yaml
helm repo add kong https://charts.konghq.com
helm repo update kong
helm upgrade --install phase2-dp kong/kong --version 3.4.1 \
  --namespace kong-stage2 --kube-context docker-desktop \
  --values phase-2/helm/dp-values.yaml \
  --set-string env.cluster_control_plane="$(terraform -chdir=phase-2/terraform output -raw cluster_control_plane)" \
  --set-string env.cluster_server_name="$(terraform -chdir=phase-2/terraform output -raw cluster_server_name)" \
  --set-string env.cluster_telemetry_endpoint="$(terraform -chdir=phase-2/terraform output -raw cluster_telemetry_endpoint)" \
  --set-string env.cluster_telemetry_server_name="$(terraform -chdir=phase-2/terraform output -raw cluster_telemetry_server_name)"
kubectl --context docker-desktop -n kong-stage2 rollout status deployment/phase2-pii --timeout=5m
kubectl --context docker-desktop -n kong-stage2 rollout status deployment/phase2-dp-kong --timeout=5m
```

Confirm `ai-gateway-poc-phase2` is Connected, In sync, and Compatible in Konnect.
The `team_token_limits` input controls allowances. Outputs `consumer_key` and
`consumer_keys` contain private credentials; `consumer_teams` lists memberships.
Phase 2 validation selects the control plane from Terraform's `control_plane_id`
output.

Keep this port-forward running in a separate terminal:

```sh
kubectl --context docker-desktop -n kong-stage2 port-forward \
  --address 127.0.0.1 service/phase2-dp-kong-proxy 8001:80
```

## 4. Use Claude Code and Codex

Load `.env` in a terminal at the repository root, then select **one** phase.

For Phase 1:

```sh
export CLIENT_PHASE=phase-1
export ANTHROPIC_BASE_URL=http://127.0.0.1:8000/anthropic
export KONG_CLIENT_KEY="$KONG_CONSUMER_KEY"
```

For Phase 2:

```sh
export CLIENT_PHASE=phase-2
export ANTHROPIC_BASE_URL=http://127.0.0.1:8001/anthropic
export TF_DATA_DIR="$PWD/.local/phase-2/terraform-data"
export KONG_CLIENT_KEY="$(terraform -chdir=phase-2/terraform output -raw consumer_key)"
```

Prepare settings and session storage for the selected phase:

```sh
export CODEX_HOME="$PWD/.local/$CLIENT_PHASE/codex"
export CLAUDE_CONFIG_DIR="$PWD/.local/$CLIENT_PHASE/claude"
install -d -m 700 "$CODEX_HOME" "$CLAUDE_CONFIG_DIR"
cp "$CLIENT_PHASE/codex.config.toml" "$CODEX_HOME/config.toml"
```

On first launch, complete the client's theme and folder-trust prompts. For
Claude Code, accept the API key supplied in the environment.

Launch Claude Code:

```sh
env -i HOME="$HOME" PATH="$PATH" TERM="$TERM" \
  CLAUDE_CONFIG_DIR="$CLAUDE_CONFIG_DIR" CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC=1 \
  ANTHROPIC_BASE_URL="$ANTHROPIC_BASE_URL" ANTHROPIC_API_KEY="${KONG_CLIENT_KEY:?}" \
  KONG_CLIENT_KEY="$KONG_CLIENT_KEY" GITHUB_TOKEN="${GITHUB_TOKEN:?}" \
  claude --bare --model claude-sonnet-5 --effort low \
  --mcp-config "$CLIENT_PHASE/.mcp.json" --strict-mcp-config \
  --tools 'mcp__github__get_file_contents'
```

Exit Claude Code before launching Codex:

```sh
env -i HOME="$HOME" PATH="$PATH" TERM="$TERM" \
  CODEX_HOME="$CODEX_HOME" KONG_CLIENT_KEY="${KONG_CLIENT_KEY:?}" \
  GITHUB_AUTHORIZATION="Bearer ${GITHUB_TOKEN:?}" \
  codex --strict-config
```

Try these prompts in **each client**, approving the GitHub read when prompted:

1. `What is two plus two? Reply with only the numeral.`
2. `Use the github tool to read the root readme of the public repository github/gitignore. Summarize its purpose in one sentence.`

Repeat with the other phase. A `401` means a missing or invalid Consumer key;
`403` indicates missing team/tool permission; `429` means an exhausted team/provider
allowance. Only the original developer has MCP access.

## 5. View traffic in Konnect

Open **Observability → Dashboards** and use an AI Gateway dashboard. In a new
organization, create one from the AI Gateway template. Filter **Control plane**
to `ai-gateway-poc-phase1` or `ai-gateway-poc-phase2` and select the window
containing your requests. Allow a short delay for analytics ingestion.

Model/provider and status charts show traffic. Consumer token usage shows
individual attribution; filter to a team's member Consumers for a team view.
Consumer Groups enforce budgets but do not automatically aggregate dashboard
usage. Explorer provides Route, Consumer, provider, and model detail.

The [Phase 2 team-usage dashboard guide](phase-2/observability/README.md) provides
a saved native Konnect dashboard and a recipe for team totals, individual
developer totals, and usage by provider. See the
[team and individual usage results](RESULTS.md#team-and-individual-usage) for the
verified example.

Stop Phase 1 with `docker compose -f phase-1/compose.yaml down`. Stop the Phase 2
port-forward with Ctrl-C. Konnect retains the configuration.
