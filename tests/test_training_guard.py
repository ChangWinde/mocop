import json
import os
import subprocess
import tempfile
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from unittest.mock import patch
from urllib.error import HTTPError

from mocop import training_guard as guard
from mocop.training_health import DEFAULTS, evaluate_health, guard_status, load_policy

NOW = 1_900_000_000_000


def snapshot(**changes):
    return {
        "health": {},
        "jobs": [],
        "monitor_started_at": NOW - 600000,
        "sampled_at": NOW - 20000,
        "list_sampled_at": NOW - 40000,
        "attempted_at": NOW - 10000,
        "complete": True,
        "error_status": None,
        "overview": {
            "running_jobs": 2,
            "utilization_jobs": 2,
            "unknown_active_card_jobs": 0,
        },
        "browser": {"status": "ok", "sampled_at": NOW - 20000},
        **changes,
    }


def policy(**changes):
    return {
        **DEFAULTS,
        "enabled": True,
        "browser_required": True,
        "auto_restart_monitor": True,
        "auto_open_browser": True,
        "desktop_notifications": True,
        "service_label": "com.example.mocop",
        **changes,
    }


class TrainingHealthTests(unittest.TestCase):
    def test_fresh_data_and_startup_grace(self):
        self.assertEqual(
            evaluate_health(snapshot(), policy(), now=NOW)["state"], "healthy"
        )
        data = snapshot(
            sampled_at=None,
            list_sampled_at=None,
            attempted_at=None,
            monitor_started_at=NOW - 1000,
            complete=False,
        )
        self.assertEqual(evaluate_health(data, policy(), now=NOW)["state"], "warming")
        self.assertFalse(
            evaluate_health(data, policy(), now=NOW)["restart_recommended"]
        )

    def test_stalled_collector_and_stale_list_are_independent_of_cached_stale_flag(
        self,
    ):
        data = snapshot(
            stale=False, list_sampled_at=NOW - 600000, attempted_at=NOW - 600000
        )
        health = evaluate_health(data, policy(), now=NOW)
        self.assertEqual(
            {i["code"] for i in health["issues"]},
            {"TASK_LIST_STALE", "COLLECTOR_STALLED"},
        )
        self.assertTrue(health["restart_recommended"])

    def test_auth_migration_and_throttle_never_recommend_restart(self):
        for error, code in (
            (302, "PLATFORM_MOVED"),
            (401, "PLATFORM_AUTH_REQUIRED"),
            (403, "PLATFORM_AUTH_REQUIRED"),
            (429, "PLATFORM_RATE_LIMITED"),
        ):
            with self.subTest(error=error):
                health = evaluate_health(
                    snapshot(
                        error_status=error,
                        attempted_at=NOW - 600000,
                        browser={"status": "platform_tab_closed", "sampled_at": NOW},
                    ),
                    policy(),
                    now=NOW,
                )
                self.assertIn(code, [i["code"] for i in health["issues"]])
                self.assertFalse(health["restart_recommended"])
                self.assertFalse(health["browser_open_recommended"])

    def test_missing_metrics_specs_browser_and_guard_are_visible(self):
        data = snapshot(
            overview={
                "running_jobs": 2,
                "utilization_jobs": 1,
                "unknown_active_card_jobs": 1,
            },
            browser={"status": "platform_tab_closed", "sampled_at": NOW - 10000},
        )
        health = evaluate_health(data, policy(), now=NOW, include_guard=True)
        self.assertEqual(
            {i["code"] for i in health["issues"]},
            {
                "GPU_METRICS_MISSING",
                "GPU_SPECS_MISSING",
                "BROWSER_OBSERVATION_UNAVAILABLE",
                "INDEPENDENT_GUARD_STALE",
            },
        )
        self.assertTrue(health["browser_open_recommended"])
        data["browser"] = {"status": "session_or_schema_unavailable", "sampled_at": NOW}
        self.assertFalse(
            evaluate_health(data, policy(), now=NOW)["browser_open_recommended"]
        )

    def test_optional_browser_does_not_warn_on_a_deployment_with_direct_api_specs(self):
        data = snapshot(browser={"status": "not_configured"})
        self.assertEqual(
            evaluate_health(data, policy(browser_required=False), now=NOW)["state"],
            "healthy",
        )

    def test_private_policy_rejects_credential_forwarding_and_bad_thresholds(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "policy.json"
            self.assertFalse(load_policy(path)["enabled"])
            for change in (
                {"url": "http://evil.example.test"},
                {"url": "http://127.0.0.1/path"},
                {"url": "http://user:pass@127.0.0.1"},
                {"enabled": "yes"},
                {"confirm_checks": 0},
                {"unknown": True},
            ):
                path.write_text(json.dumps({**policy(), **change}))
                path.chmod(0o600)
                self.assertTrue(load_policy(path)["configuration_error"])
                self.assertFalse(load_policy(path)["auto_restart_monitor"])
            path.write_text(json.dumps(policy()))
            path.chmod(0o600)
            self.assertTrue(load_policy(path)["enabled"])
            path.chmod(0o644)
            self.assertTrue(load_policy(path)["configuration_error"])
            link = Path(directory) / "linked"
            link.symlink_to(path)
            self.assertTrue(load_policy(link)["configuration_error"])


class GuardDecisionTests(unittest.TestCase):
    def test_confirm_deduplicate_recovery_and_restart_budget_survive_state_roundtrips(
        self,
    ):
        p = policy()
        health = evaluate_health(
            snapshot(list_sampled_at=NOW - 600000, attempted_at=NOW - 600000),
            p,
            now=NOW,
        )
        first, actions = guard.plan_actions({}, health, p, now=NOW)
        self.assertEqual(actions, [])
        second, actions = guard.plan_actions(
            json.loads(json.dumps(first)), health, p, now=NOW + 60000
        )
        self.assertEqual(actions, ["restart_monitor", "notify_problem"])
        third, actions = guard.plan_actions(second, health, p, now=NOW + 120000)
        self.assertEqual(actions, [])
        self.assertEqual(len(third["restarts"]), 1)
        healthy = evaluate_health(snapshot(), p, now=NOW)
        fourth, actions = guard.plan_actions(third, healthy, p, now=NOW + 180000)
        self.assertEqual(actions, ["notify_recovery"])
        _, actions = guard.plan_actions(fourth, healthy, p, now=NOW + 240000)
        self.assertEqual(actions, [])
        capped = {
            "consecutive_failures": 5,
            "restarts": [NOW - 3600000, NOW - 2400000, NOW - 1200000],
        }
        _, actions = guard.plan_actions(capped, health, p, now=NOW)
        self.assertNotIn("restart_monitor", actions)

    def test_browser_reopen_is_bounded_and_not_used_for_a_login_failure(self):
        p = policy()
        health = evaluate_health(
            snapshot(browser={"status": "platform_tab_closed", "sampled_at": NOW}),
            p,
            now=NOW,
        )
        current, actions = guard.plan_actions(
            {"consecutive_failures": 1}, health, p, now=NOW
        )
        self.assertIn("open_platform", actions)
        _, actions = guard.plan_actions(current, health, p, now=NOW + 60000)
        self.assertNotIn("open_platform", actions)
        capped = {
            "consecutive_failures": 3,
            "browser_opens": [NOW - 3600000, NOW - 2400000, NOW - 1200000],
        }
        _, actions = guard.plan_actions(capped, health, p, now=NOW)
        self.assertNotIn("open_platform", actions)
        health = evaluate_health(
            snapshot(
                browser={"status": "session_or_schema_unavailable", "sampled_at": NOW}
            ),
            p,
            now=NOW,
        )
        _, actions = guard.plan_actions({"consecutive_failures": 2}, health, p, now=NOW)
        self.assertNotIn("open_platform", actions)

    def test_authentication_and_extension_loss_alarm_without_destructive_recovery(self):
        p = policy()
        for status in (302, 401, 429):
            health = evaluate_health(
                snapshot(error_status=status, attempted_at=NOW - 600000), p, now=NOW
            )
            _, actions = guard.plan_actions(
                {"consecutive_failures": 1}, health, p, now=NOW
            )
            self.assertNotIn("restart_monitor", actions)
            self.assertIn("notify_problem", actions)
        data = snapshot()
        data.pop("health")
        result = guard.assess(p, now=NOW, reader=lambda _: data)
        self.assertEqual(result["state"], "critical")
        self.assertFalse(result["restart_recommended"])

    def test_failure_reserves_cooldown_before_side_effect_and_recovers(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            p = root / "policy.json"
            state = root / "state.json"
            p.write_text(json.dumps(policy()))
            p.chmod(0o600)
            stalled = snapshot(list_sampled_at=NOW - 600000, attempted_at=NOW - 600000)
            effects = []

            def execute(action, _, health):
                saved = json.loads(state.read_text())
                if action == "restart_monitor":
                    self.assertEqual(saved["last_restart_at"], NOW + 60000)
                    self.assertEqual(len(saved["restarts"]), 1)
                effects.append(action)
                return "failed"

            guard.run_once(
                p, state, now=NOW, reader=lambda _: stalled, executor=execute
            )
            guard.run_once(
                p, state, now=NOW + 60000, reader=lambda _: stalled, executor=execute
            )
            guard.run_once(
                p, state, now=NOW + 120000, reader=lambda _: stalled, executor=execute
            )
            self.assertEqual(effects, ["restart_monitor", "notify_problem"])
            guard.run_once(
                p,
                state,
                now=NOW + 180000,
                reader=lambda _: snapshot(
                    list_sampled_at=NOW + 160000,
                    attempted_at=NOW + 170000,
                    browser={"status": "ok", "sampled_at": NOW + 170000},
                ),
                executor=execute,
            )
            self.assertEqual(effects[-1], "notify_recovery")
            self.assertEqual(state.stat().st_mode & 0o777, 0o600)
            self.assertEqual(guard_status(state)["state"], "healthy")

    def test_local_http_errors_do_not_trigger_a_restart(self):
        p = policy()
        for status in (401, 403, 404, 503):

            def reader(_, status=status):
                raise HTTPError("http://127.0.0.1:1", status, "failure", {}, None)

            health = guard.assess(p, now=NOW, reader=reader)
            self.assertFalse(health["restart_recommended"])

        def unavailable(_):
            raise ConnectionRefusedError()

        self.assertTrue(
            guard.assess(p, now=NOW, reader=unavailable)["restart_recommended"]
        )

    def test_mac_actions_target_only_owned_service_and_default_profile(self):
        p = policy()
        health = {"issues": [{"message": 'quote " & $(ignored)'}]}
        with (
            patch.object(guard.sys, "platform", "darwin"),
            patch.object(guard.subprocess, "run") as run,
        ):
            run.return_value.returncode = 0
            self.assertEqual(guard.perform("restart_monitor", p, health), "requested")
            self.assertEqual(
                run.call_args.args[0],
                [
                    "launchctl",
                    "kill",
                    "SIGTERM",
                    f"gui/{os.getuid()}/com.example.mocop",
                ],
            )
            self.assertEqual(guard.perform("notify_problem", p, health), "accepted")
            command = run.call_args.args[0]
            self.assertNotIn("$(ignored)", command[2])
            self.assertIn("$(ignored)", command[3])
            self.assertEqual(
                guard.perform(
                    "restart_monitor", policy(service_label="bad;command"), health
                ),
                "invalid_service_label",
            )
        with (
            patch.object(guard.sys, "platform", "darwin"),
            patch.object(
                guard, "read_origin", return_value="https://training.example.test"
            ),
            patch.object(guard.subprocess, "Popen") as start,
        ):
            self.assertEqual(guard.perform("open_platform", p, health), "requested")
            args = start.call_args.args[0]
            self.assertIn("--profile-directory=Default", args)
            self.assertEqual(args[-1], "https://training.example.test/my/jobs")
            self.assertTrue(any(a.startswith("--user-data-dir=") for a in args))


class GuardHTTPTests(unittest.TestCase):
    def test_real_loopback_contract_never_marks_an_attended_dashboard_or_follows_redirects(
        self,
    ):
        received = []

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *_):
                pass

            def do_GET(self):
                received.append(dict(self.headers))
                if self.path == "/api/training-jobs" and len(received) == 1:
                    body = json.dumps(snapshot()).encode()
                    self.send_response(200)
                    self.end_headers()
                    self.wfile.write(body)
                else:
                    self.send_response(302)
                    self.send_header("Location", "http://127.0.0.1:1/other")
                    self.end_headers()

        server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            with tempfile.TemporaryDirectory() as directory:
                token = Path(directory) / "token"
                token.write_text("fictional-capability")
                token.chmod(0o600)
                p = policy(url=f"http://127.0.0.1:{server.server_port}")
                self.assertEqual(
                    guard.read_snapshot(p, token)["overview"]["running_jobs"], 2
                )
                self.assertEqual(
                    received[0]["Authorization"], "Bearer fictional-capability"
                )
                self.assertNotIn("X-Monitor-Request", received[0])
                with self.assertRaises(HTTPError):
                    guard.read_snapshot(p, token)
                self.assertEqual(len(received), 2)
                token.chmod(0o644)
                with self.assertRaises(ValueError):
                    guard.read_snapshot(p, token)
                token.unlink()
                result = guard.assess(
                    p, now=NOW, reader=lambda p: guard.read_snapshot(p, token)
                )
                self.assertFalse(result["restart_recommended"])
                self.assertEqual(result["issues"][0]["code"], "GUARD_INPUT_INVALID")
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=2)


class StandaloneGuardTests(unittest.TestCase):
    def test_guard_still_runs_without_importing_the_installed_mocop_package(self):
        import shutil
        import sys

        from mocop import platform_origin, training_health

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            for module in (guard, training_health, platform_origin):
                shutil.copy2(module.__file__, root / Path(module.__file__).name)
            # A package upgrade can remove or break Mocop; the copied guard must not import it.
            (root / "mocop.py").write_text(
                "raise RuntimeError('Mocop package unavailable')"
            )
            p = root / "policy.json"
            p.write_text(json.dumps({"enabled": False}))
            p.chmod(0o600)
            state = root / "guard.json"
            run = subprocess.run(
                [
                    sys.executable,
                    str(root / "training_guard.py"),
                    "--policy",
                    str(p),
                    "--state",
                    str(state),
                ],
                capture_output=True,
                text=True,
                timeout=15,
                env={**os.environ, "PYTHONPATH": ""},
            )
            self.assertEqual(run.returncode, 0, run.stderr)
            self.assertEqual(json.loads(run.stdout)["state"], "disabled")
            self.assertEqual(json.loads(state.read_text())["state"], "disabled")
            self.assertEqual(state.stat().st_mode & 0o777, 0o600)

    def test_disabled_or_invalid_policy_never_runs_recovery(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            p = root / "policy.json"
            state = root / "guard.json"
            p.write_text(
                json.dumps({"enabled": True, "url": "http://evil.example.test"})
            )
            p.chmod(0o600)
            with (
                patch.object(guard, "assess") as assess,
                patch.object(guard, "perform") as perform,
            ):
                result = guard.run_once(p, state, now=NOW)
            self.assertEqual(result["state"], "disabled")
            self.assertTrue(result["configuration_error"])
            assess.assert_not_called()
            perform.assert_not_called()
