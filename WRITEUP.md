# Kong AI Gateway POC

## Agreed scope

The goal is to give your engineers access to AI coding assistants while your
platform team controls access, sensitive data, and usage. We agreed to start with
a development POC that informs the end-of-quarter production decision. Discovery
identified three priorities: reduce the risk of PII and credentials leaving your
network, constrain tool actions, and attribute usage to individual developers
with a team-level view.

We agreed to demonstrate authenticated coding-assistant traffic through Kong,
multiple approved provider/model combinations, team allowances, individual usage
visibility with team rollups, input redaction, and one narrowly approved,
non-destructive GitHub MCP read. API keys provide individual access for the POC;
enterprise identity and automated group provisioning are part of production
adoption. A membership-based usage rollup demonstrates the reporting model.

We clarified that multi-provider support does not make provider interfaces
interchangeable. Each client must use a compatible native endpoint. The delivered
POC uses Claude Code with Anthropic Messages and Codex with OpenAI Responses,
with consistent policies on separate Routes. The POC demonstrates Anthropic and
OpenAI; Bedrock integration is outside this delivery. Redaction and provider
retention agreements provide separate controls; the POC evaluates redaction
behavior, including its limitations.

Phase 1 proves the flows using manual Konnect configuration and a local Docker
data plane. Phase 2 reproduces the same policies with the official
`Kong/konnect` Terraform provider and a local Kubernetes data plane deployed with
Helm. Success means real client inference and a successful approved tool read
correlated with Gateway traffic, demonstrated rejection and budget behavior, and
repeatable infrastructure. Production networking, availability, enterprise
identity, final provider policies, Bedrock support, and live Splunk/Datadog
integration remain outside the POC.

## Architecture decisions

| Decision | Why it fits |
| --- | --- |
| Hybrid deployment | Konnect manages configuration and usage views; the local data plane enforces policy and calls the local PII service before model traffic leaves the environment. Docker proves the path quickly; Kubernetes and Terraform make the configuration reproducible. |
| Provider-specific Routes | `/anthropic` preserves Messages and `/openai` preserves Responses, including streaming. AI Proxy pins the approved model and injects vaulted provider credentials instead of accepting caller overrides. |
| Separate MCP Route | `/github-mcp` handles MCP protocol and authorization separately from model inference. This prevents overlapping model and MCP plugin responsibilities. |
| `passthrough-listener` | GitHub already exposes a remote MCP server. Kong can inspect discovery and tool invocations and apply its allowlist while forwarding to that server; it does not need to generate MCP tools from a REST specification. |
| Individual credentials | Each developer has a distinct Consumer key, preserving attribution and independent revocation. Route ACLs require a real team membership. Spoofed identity/group headers do not grant membership or tool access. |
| Shared team/provider allowances | Two platform developers consume the same counter; the apps team and the other provider remain independent. Each of the four allowances is 100,000 total tokens/hour. These are token controls, not dollar budgets, invoices, or a monetary billing system. |

Only the original developer can invoke `get_file_contents`; team membership
alone grants no MCP tool access. Gateway payload logging is disabled, while
statistics and MCP audits are enabled. Provider credentials stay in Config
Store/Vault; Terraform state remains private.

See Kong's [hybrid deployment documentation](https://developer.konghq.com/gateway/hybrid-mode/)
and [AI MCP Proxy modes](https://developer.konghq.com/plugins/ai-mcp-proxy/)
for the underlying deployment and protocol contracts.

## Starter-kit diagnosis

The Kubernetes Gateway initially crash-looped before connecting to Konnect.
I checked pod status, events, and the previous container's logs, then compared
the rendered Helm configuration with the Terraform outputs and Kong's
data-plane configuration requirements.

```sh
kubectl --context docker-desktop -n kong-stage2 get pods
kubectl --context docker-desktop -n kong-stage2 describe pod POD_NAME
kubectl --context docker-desktop -n kong-stage2 logs POD_NAME -c proxy --previous
```

| Before | Diagnostic evidence and change | After |
| --- | --- | --- |
| Gateway 3.4 crash-looped with `Malformed cluster endpoint address`. The starter passed raw `https://` API URLs into clustering settings. | Held the image at 3.4. Corrected control-plane and telemetry endpoints to `host:443`, used host-only SNI, enabled `konnect_mode=on`, and set `vitals=off`. [Terraform outputs](phase-2/terraform/outputs.tf) and [Helm values](phase-2/helm/dp-values.yaml) contain the corrections. | mTLS attachment and Route sync succeeded. The connection settings were corrected together; their individual effects were not independently isolated. |
| After attachment, Konnect reported a separate Gateway compatibility issue. MCP discovery exposed 27 upstream tools. | Kept the corrected connection settings and changed the Gateway image to 3.15.0.5. Checked Konnect compatibility and MCP `tools/list` again. | The node was Connected, In sync, and Compatible; discovery contained only `get_file_contents`. Compose and Helm pin this validated image. |

The endpoint/SNI requirements are described in the
[data-plane configuration reference](https://developer.konghq.com/gateway/data-plane-reference/).
The connection-setting correction resolved attachment; the image change resolved
the separate compatibility issue.

## Validation results

Both phases completed Claude Code and Codex inference and approved GitHub reads,
with corresponding traffic in Konnect. Access tests rejected invalid credentials,
unapproved models, and unauthorized tool calls. Two platform developers shared
each provider's allowance; exhausting that allowance blocked both developers
while the other team and provider remained usable.

Both nodes were Connected, In sync, and Compatible, and a refreshed Terraform
plan reported no changes. [RESULTS.md](RESULTS.md) summarizes the outcomes;
the [validation guide](VALIDATION.md) explains how to repeat the checks.

The [team and individual usage results](RESULTS.md#team-and-individual-usage) show how
developer usage reconciles to the team total; the
[dashboard guide](phase-2/observability/README.md) explains how to build that view.

## Limitations and production adoption

**Sensitive tool outputs are not protected by the native sanitizer in this POC.**
Capture tests in both phases redacted the tested ordinary messages, conversation
history, and text blocks, but left synthetic emails and passwords unchanged in
Anthropic tool results and OpenAI function-call outputs. These tests used a local
mock upstream; the diagnostic data was never sent to real providers. Keep
sensitive tool data out of this workflow until those paths have suitable controls
and validation.

The sanitizer also produces false positives: it replaced `Kong/kong` and
`README.md` with placeholders, which can change a coding assistant's instructions.
The public `github/gitignore` example remained usable. The
[redaction results](RESULTS.md#redaction-limitations) summarize the tested
structures and transformations.

Redaction removes detected data before transmission. Provider retention policies
govern data after it reaches the provider, including anything redaction misses.
`store=false` and disabled Gateway payload logging do not establish a provider
zero-data-retention agreement. Retention, training use, processing region, and
endpoint eligibility need confirmation with each provider before production use.

Local sliding counters reset on restart, can overshoot under concurrency, and do
not coordinate multiple data planes. Production adoption needs shared counters,
identity and membership management, credential rotation, protected state,
private networking, capacity testing, and availability planning. Team usage is a
membership-based rollup; the POC includes no live external observability export.
