"""Small standard-library helpers for the POC's acceptance checks."""
import copy
from datetime import datetime, timezone
import fnmatch
import hashlib
import json
import os
from pathlib import Path
import shlex
import signal
import subprocess
import time
import urllib.error
import urllib.request
import uuid

REPO = Path(__file__).resolve().parents[1]
MODELS = {"anthropic": "claude-sonnet-5", "openai": "gpt-5.6-luna"}
STATUSES = ("PASS", "KNOWN LIMITATION", "UNVERIFIED", "FAIL")


class Unverified(Exception):
    pass


def now():
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


def environment():
    env = dict(os.environ)
    path = REPO / ".env"
    if path.exists():
        for line in path.read_text().splitlines():
            line = line.strip()
            if line.startswith("export "):
                line = line[7:]
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, value = line.split("=", 1)
            parts = shlex.split(value, comments=True)
            env.setdefault(key.strip(), parts[0] if parts else "")
    return env


def require(env, *names):
    missing = [name for name in names if not env.get(name)]
    if missing:
        raise Unverified("Missing environment variables: " + ", ".join(missing))


def private_json(path, value):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    os.chmod(path.parent, 0o700)
    fd = os.open(str(path), os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    os.fchmod(fd, 0o600)
    with os.fdopen(fd, "w") as stream:
        json.dump(value, stream, indent=2, sort_keys=True)
        stream.write("\n")


def request(url, method="GET", payload=None, headers=None, timeout=60):
    headers = dict(headers or {})
    data = None
    if payload is not None:
        data = json.dumps(payload).encode()
        headers.setdefault("Content-Type", "application/json")
    req = urllib.request.Request(url, data=data, headers=headers, method=method)
    try:
        response = urllib.request.urlopen(req, timeout=timeout)
    except urllib.error.HTTPError as error:
        response = error
    with response:
        raw = response.read().decode()
        try:
            body = json.loads(raw)
        except ValueError:
            body = raw
        return response.status, {k.lower(): v for k, v in response.headers.items()}, body


def events(body):
    """Decode JSON or complete SSE frames, including multiline data fields."""
    if isinstance(body, dict):
        return [body]
    result = []
    for frame in body.replace("\r\n", "\n").split("\n\n"):
        data = "\n".join(line[5:].lstrip() for line in frame.splitlines() if line.startswith("data:"))
        if data and data != "[DONE]":
            try:
                result.append(json.loads(data))
            except ValueError:
                pass
    return result


def stream_complete(provider, body):
    data = events(body)
    if any(e.get("type") in ("error", "response.failed", "response.incomplete") or e.get("error") for e in data):
        return False
    if provider == "anthropic":
        return (any(e.get("type") == "message_start" for e in data)
                and any(e.get("type") == "message_delta" and e.get("delta", {}).get("stop_reason") for e in data)
                and bool(data) and data[-1].get("type") == "message_stop")
    return any(e.get("type") == "response.completed" and e.get("response", {}).get("status") == "completed" for e in data)


def tool_success(result):
    """A successful HTTP response or model assertion is insufficient."""
    if not isinstance(result, dict) or result.get("error") or result.get("isError") or result.get("is_error"):
        return False
    content = result.get("content", [])
    if isinstance(content, str):
        content = [{"type": "text", "text": content}]
    nonempty = False
    for block in content:
        if not isinstance(block, dict):
            continue
        text = block.get("text", "").strip()
        if text:
            try:
                nested = json.loads(text)
            except ValueError:
                nested = None
            if isinstance(nested, dict) and (nested.get("error") or nested.get("isError")):
                return False
            if text.lower().startswith(("error:", "failed to", "error executing")):
                return False
            nonempty = True
        if block.get("resource", {}).get("text"):
            nonempty = True
    return nonempty


def approved_read(arguments):
    if isinstance(arguments, str):
        try:
            arguments = json.loads(arguments)
        except ValueError:
            return False
    return (isinstance(arguments, dict) and arguments.get("owner", "").lower() == "github"
            and arguments.get("repo", "").lower() == "gitignore"
            and arguments.get("path", "").lstrip("/").lower() == "readme.md")


def client_success(client, stdout, mcp, exit_code):
    data = []
    for line in stdout.splitlines():
        try:
            data.append(json.loads(line))
        except ValueError:
            pass
    expected = "READ_OK" if mcp else "4"
    successful_calls = 0
    if client == "claude":
        final = next((e for e in reversed(data) if e.get("type") == "result"), {})
        calls = [b for e in data if e.get("type") == "assistant" for b in e.get("message", {}).get("content", []) if isinstance(b, dict) and b.get("type") == "tool_use"]
        results = [b for e in data if e.get("type") == "user" for b in e.get("message", {}).get("content", []) if isinstance(b, dict) and b.get("type") == "tool_result"]
        for call in calls:
            if call.get("name") == "mcp__github__get_file_contents" and approved_read(call.get("input")):
                successful_calls += sum(tool_success(r) for r in results if r.get("tool_use_id") == call.get("id"))
        answer = final.get("result", "").strip()
        ok = not final.get("is_error") and (bool(answer) if mcp else answer == expected)
    else:
        items = [e.get("item", {}) for e in data if e.get("type") == "item.completed"]
        answers = [i.get("text", "") for i in items if i.get("type") == "agent_message"]
        for call in items:
            if (call.get("type") == "mcp_tool_call" and call.get("server") == "github"
                    and call.get("tool") == "get_file_contents" and call.get("status") == "completed"
                    and approved_read(call.get("arguments"))
                    and not call.get("error") and tool_success(call.get("result"))):
                successful_calls += 1
        ok = bool(answers) and (bool(answers[-1].strip()) if mcp else answers[-1].strip() == expected) and any(e.get("type") == "turn.completed" for e in data)
    return exit_code == 0 and ok and (not mcp or successful_calls > 0), successful_calls


def overall(checks):
    statuses = {c["status"] for c in checks}
    return next((s for s in ("FAIL", "UNVERIFIED", "KNOWN LIMITATION") if s in statuses), "PASS")


def provenance():
    def git(*args):
        return subprocess.check_output(["git", *args], cwd=REPO, text=True).strip()
    try:
        probe = subprocess.run(["git", "rev-parse", "--show-toplevel"], cwd=REPO, capture_output=True, text=True)
        has_git = probe.returncode == 0 and Path(probe.stdout.strip()).resolve() == REPO.resolve()
    except FileNotFoundError:
        has_git = False
    base, dirty = None, None
    if has_git:
        head = subprocess.run(["git", "rev-parse", "--verify", "HEAD"], cwd=REPO, capture_output=True, text=True)
        base = head.stdout.strip() if head.returncode == 0 else None
        dirty = bool(git("status", "--porcelain"))
        files = subprocess.check_output(["git", "ls-files", "--cached", "--others", "--exclude-standard", "-z"], cwd=REPO).decode().split("\0")
    else:
        # A ZIP has no .git directory. Hash its shipped source trees without
        # hashing local credentials, Terraform caches/state, or parent folders.
        files = [name for name in ("README.md", "WRITEUP.md", "RESULTS.md", "VALIDATION.md", ".gitignore", ".env.example") if (REPO / name).is_file()]
        private_patterns = (".env*", "*.tfstate*", "*.tfplan", "*.tfvars*", "*.pem", "*.key", "*.crt", "*.pyc")
        for directory in ("scripts", "phase-1", "phase-2"):
            for path in (REPO / directory).rglob("*"):
                relative = path.relative_to(REPO)
                if (path.is_file() and not path.is_symlink()
                        and not {".local", ".terraform", ".git", "__pycache__"}.intersection(relative.parts)
                        and not any(fnmatch.fnmatch(path.name, pattern) for pattern in private_patterns)):
                    files.append(str(relative))
    hashes = {name: hashlib.sha256((REPO / name).read_bytes()).hexdigest() for name in sorted(set(files))
              if name and (REPO / name).is_file() and not name.startswith("evidence/")}
    return {"base_commit": base, "working_tree_dirty": dirty, "git_metadata_available": has_git,
            "file_sha256": hashes, "source_manifest_sha256": digest(hashes),
            "evidence_excluded_from_manifest": True}


class Report:
    def __init__(self, phase, suite):
        self.run_id = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ") + "-" + uuid.uuid4().hex[:8]
        self.local = REPO / ".local/validation" / self.run_id
        self.local.mkdir(parents=True, mode=0o700)
        self.data = {"run_id": self.run_id, "phase": phase, "suite": suite, "started_at": now(),
                     "provenance": provenance(), "checks": [], "versions": {}}

    def add(self, check, status, **details):
        assert status in STATUSES
        item = {"timestamp": now(), "phase": self.data["phase"], "check": check, "status": status, **details}
        self.data["checks"].append(item)
        print(json.dumps(item), flush=True)
        private_json(self.local / "progress.json", self.data)
        return status == "PASS"

    def check(self, name, condition, **details):
        return self.add(name, "PASS" if condition else "FAIL", **details)

    def finish(self):
        self.data.update(ended_at=now(), overall=overall(self.data["checks"]))
        self.data["counts"] = {s: sum(c["status"] == s for c in self.data["checks"]) for s in STATUSES}
        end = provenance()
        self.data["source_unchanged_during_run"] = end["source_manifest_sha256"] == self.data["provenance"]["source_manifest_sha256"]
        path = self.local / "report.json"
        private_json(path, self.data)
        print("Report: " + str(path), flush=True)
        return 1 if self.data["overall"] == "FAIL" else 2 if self.data["overall"] == "UNVERIFIED" else 0


def command(args, env, cwd, private_path, timeout=180):
    child = subprocess.Popen(args, env=env, cwd=cwd, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                             stdin=subprocess.DEVNULL, text=True, start_new_session=True)
    try:
        stdout, stderr = child.communicate(timeout=timeout)
    except BaseException:
        os.killpg(child.pid, signal.SIGTERM)
        try:
            stdout, stderr = child.communicate(timeout=10)
        except subprocess.TimeoutExpired:
            os.killpg(child.pid, signal.SIGKILL)
            stdout, stderr = child.communicate()
        private_json(private_path, {"stdout": stdout, "stderr": stderr, "exit": child.returncode})
        raise
    private_json(private_path, {"stdout": stdout, "stderr": stderr, "exit": child.returncode})
    return child.returncode, stdout


def wait_for(check, seconds=90):
    end = time.monotonic() + seconds
    while not check():
        if time.monotonic() >= end:
            raise RuntimeError("Configuration propagation timed out")
        time.sleep(3)


class Restore:
    """Register compensation before mutations; attempt every action even after failures."""
    def __init__(self, report):
        self.report, self.actions, self.records = report, [], []
        self.journal = report.local / ("recovery-" + uuid.uuid4().hex[:8] + ".json")

    def add(self, label, action, **record):
        self.actions.append((label, action))
        self.records.append({"label": label, "status": "pending", **record})
        private_json(self.journal, self.records)

    def __enter__(self):
        return self

    def __exit__(self, *unused):
        saved = {sig: signal.signal(sig, signal.SIG_IGN) for sig in (signal.SIGINT, signal.SIGTERM)}
        failed = []
        try:
            for index in reversed(range(len(self.actions))):
                label, action = self.actions[index]
                try:
                    action()
                    self.records[index]["status"] = "restored"
                except BaseException as error:
                    self.records[index]["status"] = "FAILED"
                    self.records[index]["error_type"] = type(error).__name__
                    failed.append(label)
                private_json(self.journal, self.records)
        finally:
            for sig, handler in saved.items():
                signal.signal(sig, handler)
        self.report.check("cleanup", not failed, failed_actions=failed,
                          resource_ids=[r["id"] for r in self.records if "id" in r])
        if failed:
            raise RuntimeError("Incomplete cleanup; inspect this run's private recovery-*.json journals")


class Phase:
    def __init__(self, number, report):
        self.number, self.report = number, report
        self.name = "phase" + str(number)
        self.env = environment()
        require(self.env, "TF_VAR_konnect_personal_access_token")
        self.base = self.env.get("TF_VAR_konnect_server_url", "https://us.api.konghq.com").rstrip("/")
        if self.base not in ("https://us.api.konghq.com", "https://eu.api.konghq.com", "https://au.api.konghq.com", "https://me.api.konghq.com", "https://in.api.konghq.com"):
            raise RuntimeError("Use a documented regional Konnect API HTTPS hostname")
        self.id = self.control_plane_id()
        self.root = "/v2/control-planes/" + self.id + "/core-entities/"
        self.proxy = "http://127.0.0.1:" + str(7999 + number)
        self.routes = self.entities("routes")
        self.plugins = self.entities("plugins")
        self.consumers = self.entities("consumers")
        self.groups = self.entities("consumer_groups")
        if any((r.get("name") or r.get("username") or "").startswith("validation-") for r in self.routes + self.consumers):
            raise Unverified("Diagnostic entities remain from an earlier run; use its private recovery journal before rerunning")
        self.report.data["control_plane_id"] = self.id
        self.report.data["live_configuration_sha256"] = digest({"routes": self.routes, "plugins": self.plugins})
        private_json(report.local / "baseline.json", {"routes": self.routes, "plugins": self.plugins, "consumers": self.consumers, "groups": self.groups})
        self.credentials = self.keys()

    def control_plane_id(self):
        if self.number == 2:
            try:
                identity = self.tf(["output", "-raw", "control_plane_id"], "control-plane-id").strip()
            except (FileNotFoundError, RuntimeError) as error:
                raise Unverified("Terraform control_plane_id output is unavailable; initialize and deploy Phase 2 first") from error
            source = "Terraform control_plane_id output"
        else:
            identity = self.env.get("KONNECT_PHASE1_CONTROL_PLANE_ID", "").strip()
            source = "KONNECT_PHASE1_CONTROL_PLANE_ID"
            if not identity:
                planes = self.api("/v2/control-planes?page[size]=100")
                if planes.get("meta", {}).get("page", {}).get("total", 0) > len(planes["data"]):
                    raise Unverified("Control-plane inventory is incomplete; set KONNECT_PHASE1_CONTROL_PLANE_ID")
                matches = [p for p in planes["data"] if p["name"] == "ai-gateway-poc-phase1"]
                if len(matches) != 1:
                    raise Unverified("Expected one ai-gateway-poc-phase1 control plane; set KONNECT_PHASE1_CONTROL_PLANE_ID for an existing plane")
                identity = matches[0]["id"]
                source = "Discovered Phase 1 control-plane ID"
        try:
            identity = str(uuid.UUID(identity))
        except (ValueError, TypeError, AttributeError) as error:
            raise Unverified(source + " must contain a valid UUID") from error
        plane = self.api("/v2/control-planes/" + identity, allow_missing=True)
        if not isinstance(plane, dict) or plane.get("id") != identity:
            raise Unverified(source + " does not resolve to the expected control plane in this Konnect region")
        return identity

    def api(self, path, method="GET", payload=None, allow_missing=False):
        status, _, body = request(self.base + path, method, payload,
                                  {"Authorization": "Bearer " + self.env["TF_VAR_konnect_personal_access_token"]})
        if status >= 300 and not (allow_missing and status == 404):
            private_json(self.report.local / "api-error.json", {"status": status, "body": body})
            raise RuntimeError("Konnect " + method + " returned HTTP " + str(status))
        return body

    def core(self, path, method="GET", payload=None, **kwargs):
        return self.api(self.root + path, method, payload, **kwargs)

    def entities(self, kind):
        data = self.core(kind + "?size=1000")
        if data.get("next"):
            raise RuntimeError("Refusing to validate a partial " + kind + " inventory")
        return data["data"]

    def keys(self):
        if self.number == 2:
            return json.loads(self.tf(["output", "-json", "consumer_keys"], "consumer-keys"))
        require(self.env, "KONG_CONSUMER_KEY")
        result = {self.name + "-developer": self.env["KONG_CONSUMER_KEY"]}
        for suffix in ("-developer-2", "-app-developer"):
            consumer = next(c for c in self.consumers if c["username"] == self.name + suffix)
            keys = self.core("consumers/" + consumer["id"] + "/key-auth")["data"]
            if len(keys) != 1:
                raise Unverified("Expected one credential for " + consumer["username"])
            result[consumer["username"]] = keys[0]["key"]
        return result

    def tf(self, args, label):
        env = dict(self.env)
        for name in list(env):
            if name == "TF_CLI_ARGS" or name.startswith("TF_CLI_ARGS_"):
                env.pop(name)
        env["TF_LOG_PATH"] = str(self.report.local / (label + "-terraform.log"))
        env["TF_DATA_DIR"] = str(REPO / ".local/phase-2/terraform-data")
        if args[0] in ("plan", "apply"):
            require(env, "TF_VAR_anthropic_api_key", "TF_VAR_openai_api_key")
            cert = REPO / ".local/phase-2/cluster.crt"
            if not cert.exists():
                raise Unverified("Missing .local/phase-2/cluster.crt")
            env["TF_VAR_data_plane_client_certificate"] = cert.read_text().strip()
        code, stdout = command(["terraform", "-chdir=" + str(REPO / "phase-2/terraform"), *args], env, REPO,
                               self.report.local / (label + ".json"), timeout=240)
        if code:
            raise RuntimeError("Terraform " + args[0] + " failed; inspect private " + label + ".json")
        return stdout

    def llm(self, provider, key, stream=False, model=None, text="Reply with only the numeral 4.", headers=None):
        payload = {"model": model or MODELS[provider], "stream": stream}
        if provider == "anthropic":
            payload.update(max_tokens=64, messages=[{"role": "user", "content": text}])
            path, auth = "/anthropic/v1/messages", {"x-api-key": key, "anthropic-version": "2023-06-01"}
        else:
            payload.update(max_output_tokens=128, input=text, store=False, reasoning={"effort": "low"})
            path, auth = "/openai/v1/responses", {"apikey": key}
        auth.update(headers or {})
        return request(self.proxy + path, "POST", payload, auth)

    def create(self, kind, payload, restore):
        identity = str(uuid.uuid4())
        payload = dict(payload, id=identity)
        # Register first, including uncertain POST outcomes. DELETE is idempotent here.
        def remove():
            path = kind + "/" + identity
            self.core(path, "DELETE", allow_missing=True)
            status, _, _ = request(self.base + self.root + path,
                                    headers={"Authorization": "Bearer " + self.env["TF_VAR_konnect_personal_access_token"]})
            if status != 404:
                raise RuntimeError("Diagnostic resource deletion could not be confirmed")
        restore.add("delete-" + identity, remove, kind=kind, id=identity)
        return self.core(kind, "POST", payload)


def limiter_map(plugins, groups):
    names = {g["id"]: g["name"] for g in groups}
    result = {}
    for plugin in plugins:
        if plugin["name"] == "ai-rate-limiting-advanced":
            team = names[plugin["consumer_group"]["id"]]
            provider = plugin["config"]["llm_providers"][0]
            result.setdefault(team, {})[provider["name"]] = provider["limit"][0]
    if set(result) != {"team-platform", "team-apps"} or any(set(v) != set(MODELS) for v in result.values()):
        raise RuntimeError("Expected exactly four team/provider allowances")
    return result


def plugin_body(plugin, config):
    result = {k: copy.deepcopy(plugin[k]) for k in ("name", "instance_name", "enabled", "protocols", "route", "consumer_group", "ordering", "tags") if plugin.get(k) is not None}
    result["config"] = copy.deepcopy(config)
    return result


def same_fields(expected, actual):
    """The API may fill omitted defaults; explicitly supplied fields must match."""
    if isinstance(expected, dict):
        return isinstance(actual, dict) and all(k in actual and same_fields(v, actual[k]) for k, v in expected.items())
    return expected == actual


def guard_plan(plan, before_limits, after_limits, allowed_restore_drift=None):
    """Allow only the intended numeric allowance updates; reject unrelated drift."""
    expected = {"konnect_gateway_plugin_ai_rate_limiting_advanced.teams[\"" + t + "-" + p + "\"]": (before_limits[t][p], n)
                for t, providers in after_limits.items() for p, n in providers.items() if before_limits[t][p] != n}
    # A terminated apply can update Konnect before persisting Terraform state.
    # Compensation may reconcile only its own 1-token/original-value transition.
    for resource in plan.get("resource_drift", []):
        allowed = (allowed_restore_drift or {}).get(resource["address"])
        change = resource.get("change", {})
        if allowed is None or change["actions"] != ["update"]:
            raise RuntimeError("Terraform detected unrelated live drift")
        before, after = copy.deepcopy(change["before"]), copy.deepcopy(change["after"])
        for item in (before, after):
            if item["config"]["llm_providers"][0]["limit"][0] not in allowed:
                raise RuntimeError("Drift is outside the recorded temporary allowance")
            item["config"]["llm_providers"][0]["limit"] = []
            item.pop("updated_at", None)
        if before != after:
            raise RuntimeError("Drift changes other limiter settings")
    seen = set()
    for resource in plan.get("resource_changes", []):
        change = resource["change"]
        if change["actions"] == ["no-op"]:
            continue
        address = resource["address"]
        if address not in expected or change["actions"] != ["update"]:
            raise RuntimeError("Terraform plan contains changes outside intended limiters")
        before, after = copy.deepcopy(change["before"]), copy.deepcopy(change["after"])
        pair = (before["config"]["llm_providers"][0]["limit"][0], after["config"]["llm_providers"][0]["limit"][0])
        if pair != expected[address]:
            raise RuntimeError("Terraform allowance differs from intended values")
        before["config"]["llm_providers"][0]["limit"] = after["config"]["llm_providers"][0]["limit"]
        # The provider marks identity/timestamps computed on an in-place update.
        # Permit unknown metadata, never a concrete changed identity or policy.
        unknown = change.get("after_unknown", {})
        for field in ("id", "created_at", "updated_at"):
            if after.get(field) is None and unknown.get(field) is True:
                after[field] = before.get(field)
        def without_nulls(value):
            if isinstance(value, dict):
                return {k: without_nulls(v) for k, v in value.items() if v is not None}
            if isinstance(value, list):
                return [without_nulls(v) for v in value]
            return value
        before, after = without_nulls(before), without_nulls(after)
        if before != after:
            raise RuntimeError("Terraform plan alters other limiter settings")
        seen.add(address)
    if seen != set(expected):
        raise RuntimeError("Terraform plan omitted an intended limiter change")
    if any(c.get("actions") != ["no-op"] for c in plan.get("output_changes", {}).values()):
        raise RuntimeError("Terraform plan changes outputs")


def apply_allowances(phase, desired, label, restore_provider=None):
    current = limiter_map(phase.entities("plugins"), phase.groups)
    allowed_drift = None
    if restore_provider:
        for team, providers in desired.items():
            for provider, amount in providers.items():
                allowed = (1, amount) if (team, provider) == ("team-platform", restore_provider) else (amount,)
                if current[team][provider] not in allowed:
                    raise RuntimeError("Current allowances differ beyond the pending restoration")
        address = 'konnect_gateway_plugin_ai_rate_limiting_advanced.teams["team-platform-' + restore_provider + '"]'
        allowed_drift = {address: (1, desired["team-platform"][restore_provider])}
    path = phase.report.local / (label + ".tfplan")
    phase.tf(["plan", "-input=false", "-refresh=true", "-var=team_token_limits=" + json.dumps(desired), "-out=" + str(path)], label + "-plan")
    plan = json.loads(phase.tf(["show", "-json", str(path)], label + "-show"))
    guard_plan(plan, current, desired, allowed_drift)
    phase.tf(["apply", "-input=false", str(path)], label + "-apply")
