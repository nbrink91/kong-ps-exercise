#!/usr/bin/env python3
"""Reproduce the submitted POC checks; run phases and policy changes sequentially."""
import argparse
import copy
from datetime import datetime, timedelta, timezone
import fcntl
import json
import os
from pathlib import Path
import shutil
import signal
import sys
import time
import uuid

from capture_validation import PUBLIC_PROMPT, run_redaction
from validation_support import (MODELS, REPO, Phase, Report, Restore, Unverified, apply_allowances,
    client_success, command, digest, events, guard_plan, limiter_map, now, private_json,
    request, require, stream_complete, tool_success, wait_for, plugin_body)


def topology(phase, label):
    nodes = phase.api("/v2/control-planes/" + phase.id + "/nodes")
    expected = phase.api("/v2/control-planes/" + phase.id + "/expected-config-version")["expected_config_version"]
    if nodes.get("page", {}).get("has_next_page"):
        raise RuntimeError("Node inventory is incomplete")
    public = [{"id": n["id"], "version": n["version"], "last_ping": n["last_ping"],
               "connected": n.get("connection_state", {}).get("is_connected", False),
               "sync": n.get("config_sync"), "compatible": n.get("compatibility_status", {}).get("state"),
               "config_hash": n.get("config_hash")} for n in nodes["items"]]
    ok = len(public) == 1 and all(n["connected"] and time.time() - n["last_ping"] < 120
        and n["sync"] == {"version_id": expected, "state": "STATE_IN_SYNC"}
        and n["compatible"] == "COMPATIBILITY_STATE_FULLY_COMPATIBLE" for n in public)
    return phase.report.check(label, ok, nodes=public, expected_config_version=expected)


def remaining(headers):
    candidates = [int(v) for k, v in headers.items() if k.startswith("x-ai-ratelimit-remaining")]
    return min(candidates) if candidates else None


def headroom(phase, provider, minimum=20000):
    status, headers, _ = phase.llm(provider, phase.credentials[phase.name + "-developer"])
    amount = remaining(headers)
    if status != 200 or amount is None or amount < minimum:
        raise Unverified("Insufficient or unavailable " + provider + " allowance; wait for the sliding window, then rerun")
    phase.report.add(provider + "-allowance-preflight", "PASS", remaining_tokens=amount, required_tokens=minimum)


def run_smoke(phase):
    require(phase.env, "GITHUB_TOKEN")
    key = phase.credentials[phase.name + "-developer"]
    phase.flows = []
    for client, provider in (("claude", "anthropic"), ("codex", "openai")):
        binary = shutil.which(client)
        if not binary:
            phase.report.add(client + "-available", "UNVERIFIED", missing_executable=client)
            continue
        for mcp in (False, True):
            headroom(phase, provider)
            label = client + ("-mcp" if mcp else "-inference")
            cwd = phase.report.local / label
            cwd.mkdir(mode=0o700)
            env = {k: os.environ[k] for k in ("HOME", "PATH", "TMPDIR", "LANG", "TERM") if k in os.environ}
            env["KONG_CLIENT_KEY"] = key
            if mcp:
                env.update(GITHUB_TOKEN=phase.env["GITHUB_TOKEN"], GITHUB_AUTHORIZATION="Bearer " + phase.env["GITHUB_TOKEN"])
            prompt = PUBLIC_PROMPT if mcp else "What is two plus two? Reply with only the numeral."
            if client == "claude":
                config = cwd / "claude"
                config.mkdir(mode=0o700)
                env.update(CLAUDE_CONFIG_DIR=str(config), CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC="1",
                           ANTHROPIC_BASE_URL=phase.proxy + "/anthropic", ANTHROPIC_API_KEY=key)
                tool = "mcp__github__get_file_contents"
                args = [binary, "--bare", "-p", prompt, "--model", MODELS[provider], "--effort", "low",
                        "--tools", tool if mcp else "", "--permission-mode", "dontAsk", "--no-session-persistence",
                        "--verbose", "--output-format", "stream-json", "--max-turns", "4" if mcp else "1",
                        "--max-budget-usd", "0.30", "--strict-mcp-config"]
                if mcp:
                    args.extend(["--mcp-config", str(REPO / ("phase-" + str(phase.number)) / ".mcp.json"), "--allowedTools", tool])
            else:
                config = cwd / "codex"
                config.mkdir(mode=0o700)
                shutil.copyfile(REPO / ("phase-" + str(phase.number)) / "codex.config.toml", config / "config.toml")
                env["CODEX_HOME"] = str(config)
                args = [binary, "exec", "--strict-config", "--ephemeral", "--skip-git-repo-check", "--sandbox", "read-only", "--json",
                        "-c", "mcp_servers.github.enabled=" + ("true" if mcp else "false"), prompt]
            start = now()
            code, stdout = command(args, env, cwd, phase.report.local / (label + "-raw.json"), timeout=180)
            end = now()
            passed, calls = client_success(client, stdout, mcp, code)
            route = next(r for r in phase.routes if r["name"] == phase.name + "-" + provider)
            flow = {"label": label, "started_at": start, "ended_at": end, "provider": provider, "model": MODELS[provider],
                    "consumer_id": next(c["id"] for c in phase.consumers if c["username"] == phase.name + "-developer"),
                    "route_id": route["id"], "mcp": mcp, "client_passed": passed}
            phase.flows.append(flow)
            phase.report.check(label, passed, completed_successful_tool_calls=calls, exit_code=code, **{k: v for k, v in flow.items() if k != "label"})


class MCP:
    def __init__(self, phase, key, extra_headers=None):
        self.phase, self.seq = phase, 0
        self.headers = {"apikey": key, "Authorization": "Bearer " + phase.env["GITHUB_TOKEN"],
                        "Accept": "application/json, text/event-stream"}
        self.headers.update(extra_headers or {})
        status, result = self.call("initialize", {"protocolVersion": "2025-06-18", "capabilities": {},
                                                  "clientInfo": {"name": "kong-exercise-validation", "version": "1"}})
        if status != 200 or "result" not in result:
            raise RuntimeError("MCP initialization failed")
        protocol = result["result"].get("protocolVersion")
        if protocol not in ("2025-03-26", "2025-06-18", "2025-11-25"):
            raise RuntimeError("MCP server negotiated an unsupported protocol")
        self.headers["MCP-Protocol-Version"] = protocol
        status, _, _ = request(phase.proxy + "/github-mcp", "POST", {"jsonrpc": "2.0", "method": "notifications/initialized"}, self.headers)
        if status not in (200, 202, 204):
            raise RuntimeError("MCP initialized notification failed")

    def call(self, method, params):
        self.seq += 1
        status, headers, body = request(self.phase.proxy + "/github-mcp", "POST", {"jsonrpc": "2.0", "id": self.seq, "method": method, "params": params}, self.headers)
        if headers.get("mcp-session-id"):
            self.headers["Mcp-Session-Id"] = headers["mcp-session-id"]
        decoded = events(body)
        result = next((e for e in decoded if e.get("id") == self.seq), {})
        return status, result

    def close(self):
        if "Mcp-Session-Id" in self.headers:
            status, _, _ = request(self.phase.proxy + "/github-mcp", "DELETE", headers=self.headers)
            if status not in (200, 202, 204, 404, 405):
                raise RuntimeError("MCP session cleanup failed")


def run_access(phase):
    require(phase.env, "GITHUB_TOKEN")
    report = phase.report
    for provider in MODELS:
        for label, key in (("missing", ""), ("invalid", "validation-invalid")):
            status, _, _ = phase.llm(provider, key)
            report.check(provider + "-" + label + "-credential", status == 401, http_status=status)
    for label, headers in (("missing", {}), ("invalid", {"apikey": "validation-invalid"})):
        status, _, _ = request(phase.proxy + "/github-mcp", "POST", {"jsonrpc": "2.0", "id": 1, "method": "tools/list"}, headers)
        report.check("mcp-" + label + "-credential", status == 401, http_status=status)
    spoof = {"X-Consumer-ID": next(c["id"] for c in phase.consumers if c["username"] == phase.name + "-developer"),
             "X-Consumer-Username": phase.name + "-developer", "X-Consumer-Groups": "team-platform",
             "X-Consumer-Group": "team-platform", "X-Authenticated-Groups": "team-platform"}
    with Restore(report) as restore:
        consumer = phase.create("consumers", {"username": "validation-ungrouped-" + uuid.uuid4().hex[:12]}, restore)
        credential = phase.create("consumers/" + consumer["id"] + "/key-auth", {}, restore)
        for provider in MODELS:
            wait_for(lambda: phase.llm(provider, credential["key"])[0] != 401)
            for label, headers in (("ungrouped", {}), ("spoofed-groups", spoof)):
                status, _, _ = phase.llm(provider, credential["key"], headers=headers)
                report.check(provider + "-" + label, status == 403, http_status=status, consumer_id=consumer["id"])
        mcp = MCP(phase, credential["key"], spoof)
        try:
            status, result = mcp.call("tools/list", {})
            report.check("mcp-ungrouped-spoofed-discovery", status == 200 and result.get("result", {}).get("tools") == [],
                         http_status=status, consumer_id=consumer["id"])
            status, _ = mcp.call("tools/call", {"name": "get_file_contents", "arguments": {"owner": "github", "repo": "gitignore", "path": "README.md"}})
            report.check("mcp-ungrouped-spoofed-invocation", status == 403, http_status=status, consumer_id=consumer["id"])
        finally:
            mcp.close()
    for provider in MODELS:
        status, _, _ = phase.llm(provider, phase.credentials[phase.name + "-developer"], model="validation-unapproved-model")
        report.check(provider + "-unapproved-model", status == 400, http_status=status)
    for suffix in ("-developer", "-developer-2", "-app-developer"):
        username = phase.name + suffix
        original = suffix == "-developer"
        mcp = MCP(phase, phase.credentials[username], None if original else spoof)
        try:
            status, result = mcp.call("tools/list", {})
            names = sorted(t["name"] for t in result.get("result", {}).get("tools", []))
            report.check(username + "-mcp-discovery", status == 200 and names == (["get_file_contents"] if original else []),
                         http_status=status, tools=names, protocol=mcp.headers["MCP-Protocol-Version"])
            for tool in ("get_file_contents", "get_me", "validation_nonexistent_tool"):
                params = {"owner": "github", "repo": "gitignore", "path": "README.md"} if tool == "get_file_contents" else {}
                status, result = mcp.call("tools/call", {"name": tool, "arguments": params})
                expected_allowed = original and tool == "get_file_contents"
                ok = status == 200 and tool_success(result.get("result")) if expected_allowed else status == 403
                report.check(username + "-mcp-" + tool, ok, http_status=status, expected="allowed" if expected_allowed else "denied")
        finally:
            mcp.close()


def run_limits(phase):
    report = phase.report
    original = limiter_map(phase.entities("plugins"), phase.groups)
    private_json(report.local / "original-allowances.json", original)
    first, second, other = [phase.credentials[phase.name + suffix] for suffix in ("-developer", "-developer-2", "-app-developer")]
    for provider in MODELS:
        headroom(phase, provider, minimum=2000)
        observations = []
        for key, stream in ((first, False), (second, True), (first, False)):
            status, headers, body = phase.llm(provider, key, stream=stream)
            observations.append(remaining(headers))
            if status != 200 or (stream and not stream_complete(provider, body)):
                private_json(report.local / (provider + "-stream-failure.json"), body)
                raise RuntimeError("Budget sequence did not complete successfully")
        report.check(provider + "-shared-stream-counter", all(n is not None for n in observations)
                     and observations[0] > observations[1] > observations[2], remaining_tokens=observations,
                     consumer_usernames=[phase.name + "-developer", phase.name + "-developer-2", phase.name + "-developer"], stream_completed=True)
        reduced = copy.deepcopy(original)
        reduced["team-platform"][provider] = 1
        limiter = next(p for p in phase.plugins if p["name"] == "ai-rate-limiting-advanced"
                       and p["config"]["llm_providers"][0]["name"] == provider
                       and next(g["name"] for g in phase.groups if g["id"] == p["consumer_group"]["id"]) == "team-platform")
        with Restore(report) as restore:
            if phase.number == 2:
                restore.add("restore-" + provider + "-allowance", lambda: apply_allowances(phase, original, provider + "-restore", restore_provider=provider), original_allowances=original)
                apply_allowances(phase, reduced, provider + "-reduce")
            else:
                baseline = copy.deepcopy(limiter["config"])
                restore.add("restore-" + provider + "-allowance", lambda: phase.core("plugins/" + limiter["id"], "PUT", plugin_body(limiter, baseline)), id=limiter["id"], kind="allowance")
                config = copy.deepcopy(baseline)
                config["llm_providers"][0]["limit"] = [1]
                phase.core("plugins/" + limiter["id"], "PUT", plugin_body(limiter, config))
            wait_for(lambda: phase.llm(provider, first)[0] == 429)
            statuses = [phase.llm(provider, key)[0] for key in (first, second)]
            alternate = "openai" if provider == "anthropic" else "anthropic"
            isolated = [phase.llm(provider, other)[0], phase.llm(alternate, first)[0]]
            report.check(provider + "-shared-allowance-enforced", statuses == [429, 429] and isolated == [200, 200],
                         platform_statuses=statuses, other_team_and_provider_statuses=isolated, temporary_limit=1)
        wait_for(lambda: phase.llm(provider, first)[0] == 200)
        restored = limiter_map(phase.entities("plugins"), phase.groups)
        report.check(provider + "-allowances-restored", restored == original, allowances=restored)


def metrics(phase, start, end, provider=None, route_id=None, datasource="llm_usage", consumer_ids=None, route_ids=None):
    filters = [{"field": "control_plane", "operator": "in", "value": [phase.id]}]
    if provider:
        filters.append({"field": "ai_provider", "operator": "in", "value": [provider]})
    if route_id:
        filters.append({"field": "route", "operator": "in", "value": [phase.id + ":" + route_id]})
    if route_ids:
        filters.append({"field": "route", "operator": "in", "value": [phase.id + ":" + r for r in route_ids]})
    if consumer_ids:
        filters.append({"field": "consumer", "operator": "in", "value": [phase.id + ":" + c for c in consumer_ids]})
    metric = "total_tokens" if datasource == "llm_usage" else "request_count"
    dimensions = ["consumer", "ai_response_model"] if datasource == "llm_usage" else ["consumer", "status_code"]
    query = {"datasource": datasource, "metrics": [metric], "dimensions": dimensions, "limit": 1000,
             "time_range": {"type": "absolute", "start": start, "end": end}, "filters": filters}
    data = phase.api("/v2/metrics", "POST", query)
    if data.get("meta", {}).get("truncated"):
        raise Unverified("Konnect analytics result is truncated")
    return query, data


def correlate(phase, usage=False):
    # Konnect ingests asynchronously and limits metrics to ten queries per minute.
    print("Waiting 45 seconds for Konnect analytics ingestion.", flush=True)
    time.sleep(45)
    for flow in getattr(phase, "flows", []):
        query, data = metrics(phase, flow["started_at"], flow["ended_at"], flow["provider"], flow["route_id"], consumer_ids=[flow["consumer_id"]])
        amount = sum(d["event"].get("total_tokens", 0) for d in data["data"] if d["event"].get("ai_response_model") == flow["model"])
        phase.report.add(flow["label"] + "-gateway-correlation", "PASS" if amount > 0 and flow["client_passed"] else "UNVERIFIED",
                         query=query, actual_window={k: data["meta"][k] for k in ("start", "end")}, total_tokens=amount,
                         query_id=data["meta"].get("query_id"), client_event_result=flow["client_passed"])
        time.sleep(7)
        if flow["mcp"]:
            route = next(r for r in phase.routes if r["name"] == phase.name + "-github-mcp")
            query, data = metrics(phase, flow["started_at"], flow["ended_at"], route_id=route["id"], datasource="api_usage", consumer_ids=[flow["consumer_id"]])
            count = sum(d["event"].get("request_count", 0) for d in data["data"] if str(d["event"].get("status_code")) == "200")
            phase.report.add(flow["label"] + "-mcp-gateway-correlation", "PASS" if count > 0 and flow["client_passed"] else "UNVERIFIED",
                             query=query, http_200_requests=count, query_id=data["meta"].get("query_id"),
                             actual_window={k: data["meta"][k] for k in ("start", "end")})
            time.sleep(7)
    if usage:
        membership = []
        for group in phase.groups:
            data = phase.core("consumer_groups/" + group["id"] + "/consumers")
            if data.get("next"):
                raise Unverified("Consumer Group membership result is truncated")
            for c in data.get("consumers", data.get("data", [])):
                membership.append({"consumer_id": c["id"], "username": c["username"], "team": group["name"]})
        platform = [m for m in membership if m["team"] == "team-platform"]
        # Use a closed minute so later traffic cannot change this sample's bucket.
        end = datetime.now(timezone.utc).replace(second=0, microsecond=0).isoformat()
        managed = [r["id"] for r in phase.routes if r["name"] in (phase.name + "-anthropic", phase.name + "-openai")]
        query, data = metrics(phase, phase.report.data["started_at"], end, consumer_ids=[m["consumer_id"] for m in platform], route_ids=managed)
        totals = {m["consumer_id"]: 0 for m in platform}
        for row in data["data"]:
            identity = row["event"]["consumer"].split(":")[-1]
            if identity in totals:
                totals[identity] += row["event"]["total_tokens"]
        aggregate_query = dict(query, dimensions=[])
        time.sleep(7)
        aggregate = phase.api("/v2/metrics", "POST", aggregate_query)
        team_total = sum(r["event"]["total_tokens"] for r in aggregate["data"])
        matched_windows = all(aggregate["meta"][k] == data["meta"][k] for k in ("start", "end"))
        phase.report.check("usage-team-reconciliation", len(totals) == 2 and all(v > 0 for v in totals.values())
                           and not aggregate["meta"].get("truncated") and matched_windows and team_total == sum(totals.values()),
                           query=query, query_id=data["meta"].get("query_id"), membership=membership,
                           aggregate_query=aggregate_query, aggregate_query_id=aggregate["meta"].get("query_id"),
                           individual_total_tokens=totals, team_total_tokens=team_total,
                           reconciliation_difference=team_total - sum(totals.values()),
                           actual_window={k: data["meta"][k] for k in ("start", "end")})


def final_plan(phase, label="final-provider-refreshed-plan"):
    path = phase.report.local / (label + ".tfplan")
    phase.tf(["plan", "-input=false", "-refresh=true", "-out=" + str(path)], label)
    plan = json.loads(phase.tf(["show", "-json", str(path)], label + "-show"))
    limits = limiter_map(phase.entities("plugins"), phase.groups)
    guard_plan(plan, limits, limits)
    phase.report.add(label, "PASS", refresh=True, resource_changes=0, plan_sha256=__import__("hashlib").sha256(path.read_bytes()).hexdigest())


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--phase", type=int, choices=(1, 2), required=True)
    parser.add_argument("--suite", choices=("smoke", "access", "limits", "redaction", "all"), default="smoke")
    parser.add_argument("--allow-temporary-changes", action="store_true")
    args = parser.parse_args()
    if args.suite != "smoke" and not args.allow_temporary_changes:
        parser.error("This suite requires --allow-temporary-changes (diagnostic entities or allowance changes)")
    os.umask(0o077)
    lock_path = REPO / ".local/validation/run.lock"
    lock_path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    with lock_path.open("w") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            parser.error("Another validation phase is running; run phases sequentially")
        report = Report(args.phase, args.suite)
        def interrupted(signum, frame):
            raise KeyboardInterrupt()
        for sig in (signal.SIGINT, signal.SIGTERM):
            signal.signal(sig, interrupted)
        phase = None
        try:
            phase = Phase(args.phase, report)
            report.data["versions"]["python"] = sys.version.split()[0]
            for name, flags in (("claude", ["--version"]), ("codex", ["--version"]), ("terraform", ["version", "-json"]), ("helm", ["version", "--short"])):
                if shutil.which(name):
                    code, output = command([name, *flags], os.environ.copy(), REPO, report.local / (name + "-version.json"), timeout=20)
                    if code == 0:
                        if name == "terraform":
                            v = json.loads(output)
                            report.data["versions"].update(terraform=v["terraform_version"])
                        else:
                            report.data["versions"][name] = output.strip().splitlines()[0]
            if not topology(phase, "initial-node-state"):
                raise Unverified("Data plane is not ready for validation")
            if args.phase == 2 and args.suite in ("limits", "all"):
                final_plan(phase, "baseline-provider-refreshed-plan")
            suites = ("smoke", "access", "limits", "redaction") if args.suite == "all" else (args.suite,)
            for suite in suites:
                print("Running " + suite + " sequentially.", flush=True)
                {"smoke": run_smoke, "access": run_access, "limits": run_limits, "redaction": run_redaction}[suite](phase)
            if args.suite in ("smoke", "all"):
                correlate(phase, usage=args.suite == "all")
            report.add("suite-completed", "PASS", suites=list(suites))
        except Unverified as error:
            report.add("suite-incomplete", "UNVERIFIED", reason=str(error))
        except KeyboardInterrupt:
            report.add("suite-interrupted", "UNVERIFIED", reason="Interrupted; registered restoration actions were attempted")
        except Exception as error:
            private_json(report.local / "exception.json", {"type": type(error).__name__, "message": str(error)})
            report.add("suite-incomplete", "FAIL", error_type=type(error).__name__, diagnostic_file="exception.json")
        finally:
            if phase:
                try:
                    original = limiter_map(phase.plugins, phase.groups)
                    report.check("final-allowances", limiter_map(phase.entities("plugins"), phase.groups) == original, original_allowances=original)
                    # Compare IDs and policy fields, ignoring server-managed modification timestamps.
                    def normalized(items):
                        return {i["id"]: {k: v for k, v in i.items() if k not in ("updated_at", "created_at")} for i in items}
                    report.check("managed-routes-and-policies-restored", normalized(phase.entities("routes")) == normalized(phase.routes)
                                 and normalized(phase.entities("plugins")) == normalized(phase.plugins))
                    wait_for(lambda: phase.api("/v2/control-planes/" + phase.id + "/nodes")["items"][0].get("config_sync", {}).get("state") == "STATE_IN_SYNC")
                    topology(phase, "final-node-state")
                    for provider in MODELS:
                        status, _, _ = phase.llm(provider, phase.credentials[phase.name + "-developer"])
                        report.check(provider + "-normal-traffic-after-validation", status == 200, http_status=status)
                    if args.phase == 2:
                        final_plan(phase)
                except Exception as error:
                    private_json(report.local / "final-verification-error.json", {"type": type(error).__name__, "message": str(error)})
                    report.add("final-verification", "UNVERIFIED" if isinstance(error, Unverified) else "FAIL", error_type=type(error).__name__)
            report.data["scope_note"] = "Only selected checks were run. Native sanitizer tool-result gaps and false positives remain adoption limitations; a smoke pass is not complete sensitive-data coverage."
        return report.finish()


if __name__ == "__main__":
    sys.exit(main())
