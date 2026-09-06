"""Offline tests for validation accuracy and cleanup."""
import copy
from contextlib import redirect_stdout
import io
import json
import os
from pathlib import Path
import signal
import subprocess
import sys
import tempfile
import time
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from validation_support import Phase, Restore, Unverified, client_success, guard_plan, overall, stream_complete, tool_success, same_fields
from capture_validation import EMAIL, PASSWORD, coverage_status
import capture_validation
import validation_support


class EvidenceTests(unittest.TestCase):
    def test_zip_provenance_does_not_invent_a_commit_or_hash_local_credentials(self):
        with tempfile.TemporaryDirectory() as local:
            root = Path(local)
            (root / "scripts").mkdir()
            (root / "scripts/check.py").write_text("print('source')")
            (root / "RESULTS.md").write_text("POC results")
            (root / "scripts/.env").write_text("private")
            (root / "phase-2/.terraform").mkdir(parents=True)
            (root / "phase-2/.terraform/cache").write_text("private")
            (root / ".env").write_text("private")
            with patch.object(validation_support, "REPO", root):
                result = validation_support.provenance()
            self.assertIsNone(result["base_commit"])
            self.assertIsNone(result["working_tree_dirty"])
            self.assertFalse(result["git_metadata_available"])
            self.assertEqual(set(result["file_sha256"]), {"scripts/check.py", "RESULTS.md"})

    def test_final_report_stays_private_without_changing_result_status(self):
        for status, exit_code in (("PASS", 0), ("KNOWN LIMITATION", 0), ("UNVERIFIED", 2), ("FAIL", 1)):
            with self.subTest(status=status), tempfile.TemporaryDirectory() as local:
                root = Path(local)
                with patch.object(validation_support, "REPO", root), \
                        patch.object(validation_support, "provenance", return_value={"source_manifest_sha256": "test-manifest"}), \
                        redirect_stdout(io.StringIO()) as output:
                    report = validation_support.Report(1, "smoke")
                    report.add("test-result", status)
                    self.assertEqual(report.finish(), exit_code)
                path = root / ".local/validation" / report.run_id / "report.json"
                self.assertEqual(path.stat().st_mode & 0o777, 0o600)
                self.assertEqual(path.parent.stat().st_mode & 0o777, 0o700)
                result = json.loads(path.read_text())
                self.assertEqual(result["overall"], status)
                self.assertEqual(result["counts"][status], 1)
                self.assertTrue(result["source_unchanged_during_run"])
                self.assertIn(str(path), output.getvalue())
                self.assertFalse((root / "evidence").exists())

    def test_server_defaults_do_not_hide_an_explicit_policy_difference(self):
        self.assertTrue(same_fields({"options": {"upstream_url": "local"}}, {"options": {"upstream_url": "local", "temperature": None}}))
        self.assertFalse(same_fields({"options": {"upstream_url": "local"}}, {"options": {"upstream_url": "vendor", "temperature": None}}))

    def test_mcp_error_is_not_success(self):
        for value in ({"isError": True, "content": [{"text": "not allowed"}]},
                      {"content": [{"text": '{"error":"denied"}'}]},
                      {"content": [{"text": "Error: file not found"}]}, {"content": []}, None):
            self.assertFalse(tool_success(value))
        self.assertTrue(tool_success({"content": [{"type": "text", "text": "public repository contents"}]}))

    def test_codex_completed_failed_call_and_hallucinated_answer(self):
        call = {"type": "mcp_tool_call", "server": "github", "tool": "get_file_contents", "status": "completed",
                "arguments": {"owner": "github", "repo": "gitignore", "path": "README.md"},
                "result": {"isError": True, "content": [{"text": "denied"}]}}
        events = [{"type": "item.completed", "item": call},
                  {"type": "item.completed", "item": {"type": "agent_message", "text": "READ_OK"}}, {"type": "turn.completed"}]
        self.assertFalse(client_success("codex", "\n".join(map(json.dumps, events)), True, 0)[0])
        call["result"] = {"content": [{"text": "file contents"}]}
        self.assertTrue(client_success("codex", "\n".join(map(json.dumps, events)), True, 0)[0])
        self.assertFalse(client_success("codex", "\n".join(map(json.dumps, events[1:])), True, 0)[0])
        call["arguments"]["path"] = ""
        self.assertFalse(client_success("codex", "\n".join(map(json.dumps, events)), True, 0)[0])

    def test_claude_requires_matched_nonempty_result(self):
        events = [
            {"type": "assistant", "message": {"content": [{"type": "tool_use", "name": "mcp__github__get_file_contents", "id": "a", "input": {"owner": "github", "repo": "gitignore", "path": "README.md"}}]}},
            {"type": "user", "message": {"content": [{"type": "tool_result", "tool_use_id": "b", "content": "file contents"}]}},
            {"type": "result", "result": "READ_OK", "is_error": False}]
        self.assertFalse(client_success("claude", "\n".join(map(json.dumps, events)), True, 0)[0])
        result = events[1]["message"]["content"][0]
        result["tool_use_id"] = "a"
        self.assertTrue(client_success("claude", "\n".join(map(json.dumps, events)), True, 0)[0])
        events[-1]["result"] = "A valid one-sentence summary of the public file."
        self.assertTrue(client_success("claude", "\n".join(map(json.dumps, events)), True, 0)[0])
        result["content"] = ""
        self.assertFalse(client_success("claude", "\n".join(map(json.dumps, events)), True, 0)[0])

    def test_stream_must_have_terminal_success(self):
        def sse(*items):
            return "".join("data: " + json.dumps(i) + "\n\n" for i in items)
        start = {"type": "message_start"}
        delta = {"type": "message_delta", "delta": {"stop_reason": "end_turn"}}
        stop = {"type": "message_stop"}
        self.assertTrue(stream_complete("anthropic", sse(start, delta, stop)))
        self.assertFalse(stream_complete("anthropic", sse(start, delta)))
        self.assertFalse(stream_complete("anthropic", sse(start, {"type": "error"}, stop)))
        self.assertFalse(stream_complete("openai", sse({"type": "response.incomplete"})))
        self.assertFalse(stream_complete("openai", sse({"type": "response.completed", "response": {"status": "incomplete"}})))
        self.assertTrue(stream_complete("openai", sse({"type": "response.completed", "response": {"status": "completed"}})))

    def test_known_gaps_are_visible_and_zero_captures_never_pass(self):
        capture = [{"body": {"output": EMAIL + PASSWORD}}]
        self.assertEqual(coverage_status("function-output", 200, capture), "KNOWN LIMITATION")
        self.assertEqual(coverage_status("ordinary", 200, capture), "FAIL")
        self.assertEqual(coverage_status("tool-result", 200, []), "FAIL")
        self.assertEqual(coverage_status("ordinary", 200, [{"body": {}}]), "FAIL")
        self.assertEqual(overall([{"status": "PASS"}, {"status": "KNOWN LIMITATION"}]), "KNOWN LIMITATION")
        self.assertEqual(overall([{"status": "FAIL"}, {"status": "KNOWN LIMITATION"}]), "FAIL")
        self.assertEqual(overall([{"status": "UNVERIFIED"}, {"status": "KNOWN LIMITATION"}]), "UNVERIFIED")


class CaptureCloneTests(unittest.TestCase):
    def test_clone_construction_uses_source_ids_and_preserves_policy_without_instance_names(self):
        class CaptureReady(Exception):
            pass

        class LocalPhase:
            create = Phase.create

            def __init__(self, provider, instance_name, local):
                self.name = "phase1"
                self.env = {"TF_VAR_konnect_personal_access_token": "synthetic-key"}
                self.base, self.root = "https://us.api.konghq.com", "/test/"
                self.proxy = "http://127.0.0.1:8000"
                self.credentials = {"phase1-developer": "synthetic-key"}
                self.report = SimpleNamespace(local=local, add=Mock(), check=Mock())
                self.routes = [{"id": "source-route", "name": "phase1-" + provider}]
                self.plugins = []
                policies = [
                    ("key-auth", {"hide_credentials": True}),
                    ("acl", {"allow": ["team-platform", "team-apps"]}),
                    ("ai-sanitizer", {"stop_on_error": True, "host": "pii"}),
                    ("ai-proxy", {"auth": {"header_value": "synthetic-provider-key"},
                                  "model": {"provider": provider, "name": "pinned-model", "options": None}}),
                    ("ai-rate-limiting-advanced", {"namespace": "platform", "llm_providers": [{"name": provider, "limit": [100000]}]}),
                    ("ai-rate-limiting-advanced", {"namespace": "apps", "llm_providers": [{"name": provider, "limit": [100000]}]}),
                ]
                for index, (name, config) in enumerate(policies, 1):
                    plugin = {"id": "00000000-0000-4000-8000-" + str(index).zfill(12),
                              "name": name, "config": config, "enabled": True,
                              "protocols": ["http", "https"], "route": {"id": "source-route"}}
                    if instance_name != "missing":
                        plugin["instance_name"] = instance_name
                    if name == "ai-rate-limiting-advanced":
                        plugin["consumer_group"] = {"id": config["namespace"]}
                        plugin["ordering"] = {"before": {"access": ["ai-proxy"]}}
                    self.plugins.append(plugin)
                self.created, self.stored = [], {}

            def core(self, path, method="GET", payload=None, **unused):
                if method == "POST":
                    item = copy.deepcopy(payload)
                    self.created.append((path, item))
                    self.stored[path + "/" + item["id"]] = item
                    return item
                if method == "DELETE":
                    self.stored.pop(path)
                    return {}
                raise AssertionError("Unexpected local API operation")

            def entities(self, kind):
                return [item for path, item in self.stored.items() if path.startswith(kind + "/")]

        for provider in ("anthropic", "openai"):
            for instance_name in ("missing", None, "", "existing-name"):
                with self.subTest(provider=provider, instance_name=instance_name), tempfile.TemporaryDirectory() as local:
                    phase = LocalPhase(provider, instance_name, Path(local))
                    original = copy.deepcopy(phase.plugins)
                    with patch.object(capture_validation, "MODELS", {provider: "pinned-model"}), \
                            patch.object(capture_validation, "ThreadingHTTPServer"), \
                            patch.object(capture_validation.threading, "Thread"), \
                            patch.object(capture_validation, "wait_for", side_effect=CaptureReady), \
                            patch.object(capture_validation, "request") as upstream_request, \
                            patch.object(validation_support, "request", return_value=(404, {}, {})):
                        with self.assertRaises(CaptureReady):
                            capture_validation.run_redaction(phase)
                    upstream_request.assert_not_called()
                    clones = [item for kind, item in phase.created if kind == "plugins"]
                    self.assertEqual(len(clones), len(original))
                    self.assertEqual(len({p["instance_name"] for p in clones}), len(clones))
                    prefix = next(item["name"] for kind, item in phase.created if kind == "services")
                    for source, clone in zip(original, clones):
                        self.assertEqual(clone["instance_name"], prefix + "-" + source["id"])
                        self.assertEqual(clone["enabled"], source["enabled"])
                        self.assertEqual(clone["protocols"], source["protocols"])
                        self.assertEqual(clone.get("consumer_group"), source.get("consumer_group"))
                        self.assertEqual(clone.get("ordering"), source.get("ordering"))
                        expected = copy.deepcopy(source["config"])
                        if source["name"] == "ai-proxy":
                            expected["auth"]["header_value"] = "capture-placeholder" if provider == "anthropic" else "Bearer capture-placeholder"
                            endpoint = "messages" if provider == "anthropic" else "responses"
                            expected["model"]["options"] = {"upstream_url": "http://host.docker.internal:8099/v1/" + endpoint}
                        if source["name"] == "ai-rate-limiting-advanced":
                            expected["namespace"] = prefix + "-" + expected["namespace"]
                        self.assertEqual(clone["config"], expected)
                    self.assertEqual(phase.plugins, original)
                    self.assertFalse(phase.stored, "Diagnostic resources must be removed after interruption")
                    phase.report.check.assert_called_once()
                    self.assertTrue(phase.report.check.call_args.args[1])


class ControlPlaneTests(unittest.TestCase):
    identity = "48eec85e-e9e3-4701-9d8b-e0aff4fdc532"

    def phase(self, number, env=None):
        phase = Phase.__new__(Phase)
        phase.number, phase.env = number, env or {}
        phase.api, phase.tf = Mock(), Mock()
        return phase

    def test_phase2_uses_terraform_id_and_accepts_a_renamed_plane(self):
        phase = self.phase(2)
        phase.tf.return_value = self.identity + "\n"
        phase.api.return_value = {"id": self.identity, "name": "customer-renamed-plane"}
        self.assertEqual(phase.control_plane_id(), self.identity)
        phase.tf.assert_called_once_with(["output", "-raw", "control_plane_id"], "control-plane-id")
        phase.api.assert_called_once_with("/v2/control-planes/" + self.identity, allow_missing=True)

    def test_phase2_missing_or_invalid_terraform_output_is_unverified(self):
        for value in ("", "not-a-uuid", "../../another-plane", RuntimeError("private Terraform diagnostic"), FileNotFoundError()):
            with self.subTest(value=type(value).__name__):
                phase = self.phase(2)
                if isinstance(value, Exception):
                    phase.tf.side_effect = value
                else:
                    phase.tf.return_value = value
                with self.assertRaises(Unverified) as result:
                    phase.control_plane_id()
                self.assertNotIn("private Terraform diagnostic", str(result.exception))
                phase.api.assert_not_called()

    def test_phase1_explicit_id_bypasses_name_lookup(self):
        phase = self.phase(1, {"KONNECT_PHASE1_CONTROL_PLANE_ID": self.identity.upper()})
        phase.api.return_value = {"id": self.identity, "name": "existing-custom-plane"}
        self.assertEqual(phase.control_plane_id(), self.identity)
        phase.api.assert_called_once_with("/v2/control-planes/" + self.identity, allow_missing=True)
        phase.tf.assert_not_called()

    def test_phase1_invalid_explicit_id_does_not_call_api_or_echo_value(self):
        phase = self.phase(1, {"KONNECT_PHASE1_CONTROL_PLANE_ID": "private-input/../wrong-plane"})
        with self.assertRaises(Unverified) as result:
            phase.control_plane_id()
        self.assertNotIn("private-input", str(result.exception))
        phase.api.assert_not_called()

    def test_phase1_unique_exact_name_is_resolved_and_read_back(self):
        phase = self.phase(1, {"KONNECT_PHASE1_CONTROL_PLANE_ID": ""})
        phase.api.side_effect = [
            {"data": [{"id": self.identity, "name": "ai-gateway-poc-phase1"},
                      {"id": "unrelated", "name": "ai-gateway-poc-phase1-extra"}],
             "meta": {"page": {"total": 2}}},
            {"id": self.identity, "name": "ai-gateway-poc-phase1"},
        ]
        self.assertEqual(phase.control_plane_id(), self.identity)
        self.assertEqual(phase.api.call_count, 2)
        phase.api.assert_called_with("/v2/control-planes/" + self.identity, allow_missing=True)

    def test_phase1_missing_ambiguous_or_incomplete_inventory_is_unverified(self):
        plane = {"id": self.identity, "name": "ai-gateway-poc-phase1"}
        for inventory in ({"data": []}, {"data": [plane, plane]},
                          {"data": [plane], "meta": {"page": {"total": 101}}}):
            with self.subTest(inventory=inventory):
                phase = self.phase(1)
                phase.api.return_value = inventory
                with self.assertRaises(Unverified):
                    phase.control_plane_id()
                phase.api.assert_called_once_with("/v2/control-planes?page[size]=100")

    def test_missing_or_mismatched_id_readback_is_unverified(self):
        for number in (1, 2):
            for returned in ({"message": "Not found"}, {"id": "00000000-0000-4000-8000-000000000000"}):
                with self.subTest(number=number, returned=returned):
                    phase = self.phase(number, {"KONNECT_PHASE1_CONTROL_PLANE_ID": self.identity})
                    phase.tf.return_value = self.identity
                    phase.api.return_value = returned
                    with self.assertRaises(Unverified):
                        phase.control_plane_id()
                    phase.api.assert_called_once_with("/v2/control-planes/" + self.identity, allow_missing=True)


class PlanTests(unittest.TestCase):
    def setUp(self):
        self.before = {"team-platform": {"anthropic": 100000, "openai": 100000}, "team-apps": {"anthropic": 100000, "openai": 100000}}
        self.after = copy.deepcopy(self.before)
        self.after["team-platform"]["anthropic"] = 1
        old = {"id": "id", "updated_at": 1, "config": {"namespace": "stable", "llm_providers": [{"name": "anthropic", "limit": [100000]}]}}
        new = copy.deepcopy(old)
        new["config"]["llm_providers"][0]["limit"] = [1]
        new["updated_at"] = None
        self.plan = {"resource_changes": [{"address": 'konnect_gateway_plugin_ai_rate_limiting_advanced.teams["team-platform-anthropic"]',
                     "change": {"actions": ["update"], "before": old, "after": new, "after_unknown": {"updated_at": True}}}]}

    def test_only_intended_numeric_update_is_allowed(self):
        guard_plan(self.plan, self.before, self.after)
        self.plan["resource_changes"][0]["change"]["after"]["config"]["namespace"] = "resets-counter"
        with self.assertRaises(RuntimeError):
            guard_plan(self.plan, self.before, self.after)

    def test_replacement_drift_and_missing_changes_are_rejected(self):
        for field, value in (("resource_drift", [{"address": "unexpected"}]), ("resource_changes", []),
                             ("output_changes", {"consumer_key": {"actions": ["update"]}})):
            bad = copy.deepcopy(self.plan)
            bad[field] = value
            with self.assertRaises(RuntimeError):
                guard_plan(bad, self.before, self.after)
        self.plan["resource_changes"][0]["change"]["actions"] = ["delete", "create"]
        with self.assertRaises(RuntimeError):
            guard_plan(self.plan, self.before, self.after)

    def test_computed_identity_is_distinct_from_replacement_identity(self):
        change = self.plan["resource_changes"][0]["change"]
        change["after"]["id"] = None
        change["after_unknown"]["id"] = True
        guard_plan(self.plan, self.before, self.after)
        change["after"]["id"] = "different-id"
        with self.assertRaises(RuntimeError):
            guard_plan(self.plan, self.before, self.after)

    def test_partial_apply_drift_can_only_be_reconciled_for_recorded_allowance(self):
        drift = copy.deepcopy(self.plan["resource_changes"][0])
        drift["change"]["after"]["updated_at"] = 2
        self.plan["resource_drift"] = [drift]
        address = drift["address"]
        guard_plan(self.plan, self.before, self.after, {address: (1, 100000)})
        drift["change"]["after"]["config"]["namespace"] = "unrelated"
        with self.assertRaises(RuntimeError):
            guard_plan(self.plan, self.before, self.after, {address: (1, 100000)})


class CleanupTests(unittest.TestCase):
    def test_lifo_cleanup_continues_after_failure(self):
        class Report:
            def check(self, *args, **kwargs):
                self.result = args
        report = Report()
        actions = []
        with tempfile.TemporaryDirectory() as local:
            report.local = Path(local)
            def failure():
                actions.append("failed")
                raise RuntimeError()
            with self.assertRaises(RuntimeError):
                with Restore(report) as restore:
                    restore.add("first", lambda: actions.append("first"))
                    restore.add("failure", failure)
                    restore.add("last", lambda: actions.append("last"))
                    raise KeyboardInterrupt()
            self.assertEqual(actions, ["last", "failed", "first"])
            self.assertFalse(report.result[1])
            self.assertEqual(json.loads(next(Path(local).glob("recovery-*.json")).read_text())[1]["status"], "FAILED")

    def test_real_sigterm_restores_after_mutation(self):
        source = r'''
import signal, time, sys
from pathlib import Path
from validation_support import Restore
class Report:
    local = Path(sys.argv[1])
    def check(self, *args, **kwargs): pass
def stop(signum, frame): raise KeyboardInterrupt()
signal.signal(signal.SIGTERM, stop)
path = Report.local / 'allowance'
path.write_text('100000')
try:
    with Restore(Report()) as restore:
        restore.add('allowance', lambda: path.write_text('100000'))
        path.write_text('1')
        print('ready', flush=True)
        time.sleep(30)
except KeyboardInterrupt:
    pass
'''
        with tempfile.TemporaryDirectory() as local:
            env = dict(os.environ, PYTHONPATH=str(Path(__file__).resolve().parents[1]))
            child = subprocess.Popen([sys.executable, "-c", source, local], env=env, stdout=subprocess.PIPE, text=True)
            try:
                self.assertEqual(child.stdout.readline().strip(), "ready")
                child.send_signal(signal.SIGTERM)
                child.wait(timeout=5)
                self.assertEqual((Path(local) / "allowance").read_text(), "100000")
                self.assertEqual(json.loads(next(Path(local).glob("recovery-*.json")).read_text())[0]["status"], "restored")
            finally:
                if child.poll() is None:
                    child.kill()
                    child.wait()
                child.stdout.close()


if __name__ == "__main__":
    unittest.main()
