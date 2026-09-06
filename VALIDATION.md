# Validate the POC

Use these checks after deploying the POC with [README.md](README.md). See
[RESULTS.md](RESULTS.md) for demonstrated outcomes and redaction limitations.
Run from the repository root with Python 3.9+ on macOS or another POSIX
environment. Run one phase at a time in a quiet POC environment; client turns
and policy checks consume real token allowance.

## Prerequisites

- Docker Desktop and both deployed data planes; Phase 1 proxy on `127.0.0.1:8000`,
  Phase 2 port-forward on `127.0.0.1:8001`. Keep the README's port-forward running.
- Claude Code and Codex on `PATH`, with the shipped phase-specific client files.
  Client validation used Claude Code 2.1.260 and Codex 0.153.0; minimum client
  versions have not been established.
  Both real provider accounts must support the pinned models.
- A Konnect organization with AI Gateway Enterprise entitlement or an enabled
  trial that includes the AI plugins listed in the [setup prerequisites](README.md#1-prepare-credentials).
- `TF_VAR_konnect_personal_access_token` with configuration and analytics access,
  and `GITHUB_TOKEN` for the client/access suites. Phase 1 also needs
  `KONG_CONSUMER_KEY` for the original developer. Additional Phase 1 developer
  keys are read from this control plane's Key Auth records.
- Phase 1 selects `KONNECT_PHASE1_CONTROL_PLANE_ID` when set; otherwise it finds
  exactly one control plane named `ai-gateway-poc-phase1`. Phase 2 uses Terraform's
  `control_plane_id` output. Missing or ambiguous targets produce `UNVERIFIED`.
- `TF_VAR_konnect_server_url` selects the regional Konnect API for Terraform and
  both validation phases. It defaults to `https://us.api.konghq.com` and must match
  the region where you created the control planes.
- Phase 2 needs the README's initialized Terraform backend and provider cache in
  `.local/phase-2/`, `cluster.crt`, and the sensitive `consumer_keys` output.
  `TF_VAR_anthropic_api_key` and `TF_VAR_openai_api_key` are required for live
  plans.
- Local capture port `8099` must be free. Both Docker Desktop data planes must
  reach `host.docker.internal:8099`. The listener binds to loopback, and diagnostic
  AI Proxy clones use only placeholder provider credentials and this local URL.

The runner reads credentials from exported variables or the repository's `.env`,
with exported variables taking precedence. Use literal values in `.env`; shell
expressions are not evaluated. Keep the file at mode `600`. Missing prerequisites
produce an `UNVERIFIED` result naming the missing variables without their values.

## Run

```sh
python3 scripts/validate.py --phase 1 --suite smoke
python3 scripts/validate.py --phase 2 --suite all --allow-temporary-changes
```

`--suite` defaults to `smoke`. To run all checks for both phases,
run Phase 1 with `--suite all --allow-temporary-changes`, wait for it to finish,
then run Phase 2. A local file lock prevents overlapping helper runs.

| Suite | Checks and expected outcomes | Temporary effects |
| --- | --- | --- |
| `smoke` | Claude and Codex each complete inference and a GitHub read, verified from the tool result and matching Konnect traffic on the model and MCP Routes. | Four isolated client sessions plus allowance probes. No policy edits. |
| `access` | Missing/invalid credentials: `401`. Ungrouped Consumer and spoofed membership: `403`. Wrong model: `400`. Original developer discovers only `get_file_contents`; its approved read succeeds; `get_me`, fabricated tools, and other developers' calls are denied. | Creates and removes a uniquely identified Consumer and credential; creates and closes MCP sessions. |
| `limits` | Two platform developers consume one counter, including a completed stream. Reducing an allowance to one token returns `429` for both; the other team and provider still return `200`. | Temporarily reduces one allowance at a time, then restores the exact original map. Does not change namespaces or reset counters. |
| `redaction` | Captures ordinary input, history, text blocks, and tool outputs. Records redaction gaps and false positives. Wrong model and unreachable sanitizer must produce zero upstream captures. | Creates isolated Services, Routes, and plugin clones with distinct counter namespaces; makes a diagnostic sanitizer unreachable, then removes every diagnostic entity. |
| `all` | Runs the four suites in order, plus a team usage rollup from current Konnect memberships. | All effects above, sequentially. |

Before each client turn, a minimal real request reads the remaining-token header.
The helper requires at least 20,000 remaining tokens for a client turn and 2,000
for a policy check. Client turns and concurrent traffic can consume more than
this margin. If allowance is insufficient, wait for the sliding window to expire
and rerun. The helper does not raise allowances or reset counters.

Client sessions use isolated private directories and the public `github/gitignore`
MCP example. Claude gets only `mcp__github__get_file_contents`; Codex disables
shell, apps, plugins, and web search. Model-only turns disable MCP.

## Interpret results

| Status | Meaning |
| --- | --- |
| `PASS` | The specific assertion has supporting evidence. |
| `KNOWN LIMITATION` | Capture demonstrates the disclosed native redaction gap or false positive. This remains visible in the overall result. |
| `UNVERIFIED` | A prerequisite, ingestion result, or completed execution is unavailable. |
| `FAIL` | Observed behavior fails the check, the helper encounters an unexpected error, or cleanup is incomplete. |

The overall order is `FAIL`, then `UNVERIFIED`, then `KNOWN LIMITATION`, then
`PASS`. Exit codes are `1` for a failure, `2` for unverified coverage, and `0`
for passing checks with any known limitations still explicitly recorded.
A smoke result never asserts redaction
coverage. See [WRITEUP.md](WRITEUP.md) for the adoption boundary.

Run suites sequentially because Gateway correlation uses aggregate traffic
within an analytics window. Delayed ingestion produces `UNVERIFIED`; repeat the
query recorded in the local report in Explorer after ingestion completes.
Usage rollups compare both platform developers with an aggregate query over the
same membership, model Routes, and time window. They measure tokens, not money.

## Restoration and recovery

The runner saves the original configuration and records every temporary resource
ID before making changes. It restores settings and removes those resources after
success, errors, Ctrl-C, or SIGTERM. An incomplete cleanup is reported as `FAIL`.
Allow cleanup to finish before stopping the process again.

Phase 1 restores complete original plugin settings with Konnect PUT requests.
Phase 2 uses saved, provider-refreshed Terraform plans for reductions and
restoration. Plans are restricted to the intended limiter changes; unrelated
configuration changes are rejected. If an interrupted apply reached Konnect
before writing Terraform state, restoration can reconcile that recorded change.
Final checks compare the restored IDs and settings, verify node health and normal
model traffic, and finish Phase 2 with a provider-refreshed no-change plan.

SIGKILL, machine loss, or revoked credentials can prevent cleanup. In that case:

1. Stop other validation work. Inspect the affected run under
   `.local/validation/<run-id>/`. `baseline.json`, `original-allowances.json`, and
   `recovery-*.json` contain the original settings, exact resource IDs, and each
   action's completion status. Do not delete resources by a broad name filter.
2. In the affected Konnect control plane, remove only the pending diagnostic
   UUIDs from the journal: plugins first, then Routes, then Services; credentials
   before their temporary Consumer. Confirm those UUIDs no longer exist. Never
   delete an allowance entry's plugin ID: that entry requires restoration.
3. For a Phase 1 allowance, restore that plugin's original settings from
   `baseline.json` in Konnect. For Phase 2, load the README's Terraform environment,
   set `TF_VAR_team_token_limits` to the JSON in `original-allowances.json`, save a
   new `terraform plan -out=...` under `.local/`, and inspect it. Apply only if it
   restores the intended limiter values and changes nothing else. Do not apply
   a stale reduction plan. Unset the temporary variable after restoration.
4. Verify the original allowances, absence of diagnostic IDs, successful normal
   inference, and Connected/In sync/Compatible node state. Finish Phase 2 with a
   normal, provider-refreshed plan showing no changes. Rerun the relevant suite;
   retain the failed attempt's result until the new verification resolves it.

The runner ignores `TF_CLI_ARGS` overrides and keeps Terraform debug logs in the
private run folder.

## Check local configuration

```sh
python3 -m compileall -q scripts
python3 -m unittest discover -s scripts/tests -v
export TF_DATA_DIR="$PWD/.local/phase-2/terraform-data"
terraform -chdir=phase-2/terraform fmt -check -recursive
terraform -chdir=phase-2/terraform validate
helm template phase2-dp kong/kong --version 3.4.1 \
  --namespace kong-stage2 --values phase-2/helm/dp-values.yaml \
  > .local/phase-2/rendered.yaml
```

For Compose validation, load the certificate and host variables exactly as in
README, then run `docker compose -f phase-1/compose.yaml config --quiet`.
Expanded Compose output includes certificate material; keep it private.

## Local results and diagnostics

The runner prints the final report path:
`.local/validation/<run-id>/report.json`. Per-check results, source provenance,
client output, captures, snapshots, and recovery plans stay in this ignored
directory with private permissions (`700` directories, `600` files). Keep `.env`,
`.local/`, Terraform state, and client credentials out of shared archives.
