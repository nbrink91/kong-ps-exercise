# View platform-team and developer usage

The saved [AI Gateway POC Phase 2: Platform team usage dashboard](https://cloud.konghq.com/us/analytics/dashboards/a3c9031c-2147-4ce2-b1cd-94104f45c602)
shows a team total, each developer's contribution, and a breakdown by developer
and provider. It uses native Konnect dashboard tiles and a saved Consumer filter
for the platform team's membership.

## Reproduce the dashboard

1. Open **Observability → Dashboards** and select the saved dashboard. To build
   it in another deployment, create a dashboard with the same title and select
   that deployment's resources in the filters below.
2. Apply these dashboard filters:

   | Filter | Values in this POC |
   | --- | --- |
   | Control plane | `ai-gateway-poc-phase2` |
   | Route | `phase2-anthropic`, `phase2-openai` |
   | Consumer | `phase2-developer`, `phase2-developer-2` |
   | LLM provider | `anthropic`, `openai` |

3. Create the four tiles below with the **LLM** data source. Set every tile to
   **Use dashboard time** and save the dashboard.

   | Tile | Chart | Metrics | Group by | Additional tile filter |
   | --- | --- | --- | --- | --- |
   | Platform team total | Single value | Total token count | None | None |
   | `phase2-developer` usage | Single value | Total token count | None | Consumer: `phase2-developer` |
   | `phase2-developer-2` usage | Single value | Total token count | None | Consumer: `phase2-developer-2` |
   | Usage by developer and provider | Toplist | Total, Prompt, and Completion token count | Consumer, LLM provider | None |

4. Select an absolute dashboard window from **September 5, 2026, 04:17 to 04:44
   UTC** to reproduce the example below. In an Arizona browser timezone, this is
   **September 4, 21:17 to 21:44 MST**. For another deployment, select a completed
   window containing its model requests.
5. Confirm that the two developer totals sum to the team total and that prompt
   plus completion tokens equals total tokens.

## Verified example

The example uses the closed reporting window above and includes only the two
managed model Routes, both platform developers, and both approved providers.

| Measure | Tokens |
| --- | ---: |
| `phase2-developer` | 44,165 |
| `phase2-developer-2` | 80 |
| Platform team total | 44,245 |
| Prompt tokens | 43,556 |
| Completion tokens | 689 |

The developer sum and prompt/completion sum both reconcile to **44,245**. See
the [dashboard screenshot](../evidence/team-usage-dashboard.png) and
[usage results](../../RESULTS.md#team-and-individual-usage). Toplist abbreviates
larger values; the Single value tiles show the exact developer and team totals.

Keep the saved Consumer filter aligned with `team-platform` membership when
developers join or leave. Consumer Groups enforce the token allowances; this
dashboard rolls up usage for the Consumers selected in its filter.
