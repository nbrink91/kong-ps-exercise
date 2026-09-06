"""Synthetic-only capture on disposable clones. No diagnostic input goes to vendors."""
import copy
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import threading
import time
import uuid

from validation_support import MODELS, Restore, digest, private_json, request, wait_for, plugin_body, same_fields

EMAIL = "boundary.audit@example.com"
PASSWORD = "SyntheticPassword987!"
SENSITIVE = "Contact " + EMAIL + "; password: " + PASSWORD
PUBLIC_PROMPT = "Use the github tool to read the root readme of the public repository github/gitignore. Summarize its purpose in one sentence."
REFERENCE_PROMPT = "Use the github tool to read README.md from Kong/kong."


def fixtures(provider):
    if provider == "anthropic":
        base = {"model": MODELS[provider], "max_tokens": 32}
        variants = {
            "ordinary": [{"role": "user", "content": SENSITIVE}],
            "history": [{"role": "user", "content": SENSITIVE}, {"role": "assistant", "content": "Understood."}, {"role": "user", "content": "Reply 4."}],
            "text-block": [{"role": "user", "content": [{"type": "text", "text": SENSITIVE}]}],
            "tool-result": [
                {"role": "user", "content": "Read the diagnostic fixture."},
                {"role": "assistant", "content": [{"type": "tool_use", "id": "toolu_fixture", "name": "read_fixture", "input": {}}]},
                {"role": "user", "content": [{"type": "tool_result", "tool_use_id": "toolu_fixture", "content": SENSITIVE}]}],
            "false-positive": [{"role": "user", "content": REFERENCE_PROMPT}],
            "public-prompt": [{"role": "user", "content": PUBLIC_PROMPT}],
        }
        return {name: dict(base, messages=value) for name, value in variants.items()}
    base = {"model": MODELS[provider], "max_output_tokens": 32, "store": False}
    variants = {
        "ordinary": SENSITIVE,
        "history": [{"role": "user", "content": SENSITIVE}, {"role": "assistant", "content": "Understood."}, {"role": "user", "content": "Reply 4."}],
        "text-block": [{"role": "user", "content": [{"type": "input_text", "text": SENSITIVE}]}],
        "function-output": [
            {"role": "user", "content": "Read the diagnostic fixture."},
            {"type": "function_call", "call_id": "call_fixture", "name": "read_fixture", "arguments": "{}"},
            {"type": "function_call_output", "call_id": "call_fixture", "output": SENSITIVE},
            {"role": "user", "content": "Reply 4."}],
        "false-positive": REFERENCE_PROMPT,
        "public-prompt": PUBLIC_PROMPT,
    }
    return {name: dict(base, input=value) for name, value in variants.items()}


def coverage_status(name, status, captures):
    if status != 200 or len(captures) != 1:
        return "FAIL"
    captured = json.dumps(captures[0]["body"])
    leaked = any(value in captured for value in (EMAIL, PASSWORD))
    if leaked:
        return "KNOWN LIMITATION" if name in ("tool-result", "function-output") else "FAIL"
    # Absence alone could mean dropped content. Require a positive redaction marker.
    return "PASS" if "PLACEHOLDER" in captured else "FAIL"


def run_redaction(phase):
    report = phase.report
    captures, lock = [], threading.Lock()

    class Capture(BaseHTTPRequestHandler):
        def log_message(self, *unused):
            pass

        def do_POST(self):
            body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
            with lock:
                captures.append({"body": body, "headers": dict(self.headers)})
            if self.path.endswith("responses"):
                response = {"id": "resp_acceptance", "object": "response", "created_at": int(time.time()),
                            "status": "completed", "model": MODELS["openai"],
                            "output": [{"type": "message", "id": "msg_acceptance", "role": "assistant", "status": "completed",
                                        "content": [{"type": "output_text", "text": "4", "annotations": []}]}],
                            "usage": {"input_tokens": 12, "output_tokens": 1, "total_tokens": 13}}
            else:
                response = {"id": "msg_acceptance", "type": "message", "role": "assistant", "model": MODELS["anthropic"],
                            "content": [{"type": "text", "text": "4"}], "stop_reason": "end_turn",
                            "usage": {"input_tokens": 12, "output_tokens": 1}}
            raw = json.dumps(response).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(raw)))
            self.end_headers()
            self.wfile.write(raw)

    # Docker Desktop forwards host.docker.internal to this host-only listener.
    server = ThreadingHTTPServer(("127.0.0.1", 8099), Capture)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    key = phase.credentials[phase.name + "-developer"]
    try:
        for provider in MODELS:
            with Restore(report) as restore:
                source_route = next(r for r in phase.routes if r["name"] == phase.name + "-" + provider)
                sources = [p for p in phase.plugins if (p.get("route") or {}).get("id") == source_route["id"]]
                if not {"ai-proxy", "ai-sanitizer", "key-auth", "acl", "ai-rate-limiting-advanced"}.issubset({p["name"] for p in sources}):
                    raise RuntimeError("Live route is missing a required capture policy")
                prefix = "validation-" + uuid.uuid4().hex[:10] + "-" + provider
                service = phase.create("services", {"name": prefix, "host": "host.docker.internal", "port": 8099,
                                                    "protocol": "http", "retries": 0}, restore)
                route = phase.create("routes", {"name": prefix, "paths": ["/" + prefix], "strip_path": True,
                                                "protocols": ["http"], "service": {"id": service["id"]}}, restore)
                upstream = "http://host.docker.internal:8099/v1/" + ("messages" if provider == "anthropic" else "responses")
                sanitizer = None
                for source in sources:
                    body = {k: copy.deepcopy(source[k]) for k in ("name", "config", "enabled", "protocols")}
                    body.update(instance_name=prefix + "-" + source["id"], route={"id": route["id"]})
                    for field in ("consumer_group", "ordering"):
                        if source.get(field):
                            body[field] = copy.deepcopy(source[field])
                    if source["name"] == "ai-proxy":
                        body["config"]["auth"]["header_value"] = "capture-placeholder" if provider == "anthropic" else "Bearer capture-placeholder"
                        body["config"]["model"]["options"] = body["config"]["model"].get("options") or {}
                        body["config"]["model"]["options"]["upstream_url"] = upstream
                    if source["name"] == "ai-rate-limiting-advanced":
                        body["config"]["namespace"] = prefix + "-" + source["config"]["namespace"]
                    clone = phase.create("plugins", body, restore)
                    if not same_fields(body["config"], clone["config"]):
                        raise RuntimeError("Diagnostic plugin differs from requested configuration")
                    if source["name"] == "ai-sanitizer":
                        sanitizer = clone
                # Read back the upstream before ANY fixture, including the wrong-model probe.
                proxy = next(p for p in phase.entities("plugins") if (p.get("route") or {}).get("id") == route["id"] and p["name"] == "ai-proxy")
                if (proxy["config"]["model"]["options"]["upstream_url"] != upstream
                        or "capture-placeholder" not in proxy["config"]["auth"]["header_value"]):
                    raise RuntimeError("Diagnostic upstream/credential safety check failed")
                report.add(provider + "-capture-clone", "PASS", route_id=route["id"], source_route_id=source_route["id"],
                           source_policy_sha256=digest(sources), placeholder_provider_credentials=True)
                auth = {"x-api-key": key, "anthropic-version": "2023-06-01"} if provider == "anthropic" else {"apikey": key}
                url = phase.proxy + "/" + prefix + ("/v1/messages" if provider == "anthropic" else "/v1/responses")
                cases = fixtures(provider)
                probe = copy.deepcopy(cases["public-prompt"])
                probe["model"] = "validation-unapproved-model"
                wait_for(lambda: request(url, "POST", probe, auth)[0] == 400)

                def capture(payload):
                    with lock:
                        captures.clear()
                    status, _, body = request(url, "POST", payload, auth)
                    with lock:
                        seen = copy.deepcopy(captures)
                    return status, body, seen

                status, _, seen = capture(probe)
                report.check(provider + "-wrong-model-zero-upstream", status == 400 and not seen,
                             http_status=status, upstream_captures=len(seen))
                for name, payload in cases.items():
                    status, response, seen = capture(payload)
                    private_json(report.local / (provider + "-" + name + "-capture.json"), {"response": response, "captures": seen})
                    serialized = json.dumps(seen)
                    credential_leak = any(value and value in serialized for value in phase.credentials.values())
                    credential_leak |= any(phase.env.get(n) and phase.env[n] in serialized for n in ("TF_VAR_anthropic_api_key", "TF_VAR_openai_api_key"))
                    if credential_leak:
                        raise RuntimeError("Unexpected real credential at diagnostic upstream; private capture retained")
                    details = {"provider": provider, "http_status": status, "upstream_captures": len(seen), "route_id": route["id"]}
                    if name in ("false-positive", "public-prompt"):
                        field = "messages" if provider == "anthropic" else "input"
                        original = payload[field]
                        actual = seen[0]["body"].get(field) if len(seen) == 1 else None
                        if status != 200 or actual is None:
                            result = "FAIL"
                        elif name == "public-prompt":
                            result = "PASS" if actual == original else "FAIL"
                        else:
                            result = "KNOWN LIMITATION" if actual != original else "PASS"
                        # Public fixtures only. Sensitive fixture contents are never exported.
                        if actual is not None:
                            details.update(original=original, transformed=actual)
                    else:
                        result = coverage_status(name, status, seen)
                        details.update(email_present=EMAIL in serialized, password_present=PASSWORD in serialized)
                    report.add(provider + "-redaction-" + name, result, **details)
                broken = copy.deepcopy(sanitizer["config"])
                broken.update(host="127.0.0.1", port=1, timeout=1000)
                phase.core("plugins/" + sanitizer["id"], "PUT", plugin_body(sanitizer, broken))
                wait_for(lambda: request(url, "POST", cases["public-prompt"], auth)[0] >= 500)
                status, _, seen = capture(cases["ordinary"])
                report.check(provider + "-sanitizer-fail-closed", status >= 500 and not seen,
                             http_status=status, upstream_captures=len(seen), diagnostic_sanitizer_id=sanitizer["id"])
    finally:
        server.shutdown()
        server.server_close()
