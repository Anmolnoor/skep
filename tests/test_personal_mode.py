import json
import tempfile
import threading
import unittest
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from types import TracebackType
from typing import Any, cast
from unittest import mock

from skep.dashboard import render_dashboard
from skep.profile import load_profile, run_personal_setup
from skep.status import build_status, format_doctor_report
from skep.supervisor.sandbox import SandboxAvailability


class _ProviderHandler(BaseHTTPRequestHandler):
    def do_GET(self) -> None:
        if self.path == "/api/tags":
            body = b'{"models":[]}'
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return

        self.send_response(404)
        self.end_headers()

    def log_message(self, _format: str, *args: Any) -> None:
        return


class ProviderServer:
    def __init__(self) -> None:
        self.server: HTTPServer | None = None
        self.thread: threading.Thread | None = None

    def __enter__(self) -> str:
        self.server = HTTPServer(("127.0.0.1", 0), _ProviderHandler)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        host, port = cast(tuple[str, int], self.server.server_address)
        return f"http://{host}:{port}"

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        assert self.server is not None
        assert self.thread is not None
        self.server.shutdown()
        self.thread.join(timeout=2)
        self.server.server_close()


class PersonalModeTests(unittest.TestCase):
    def test_setup_creates_idempotent_personal_profile_and_storage(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp)

            first = run_personal_setup(home, provider="mock", model="local-ready")
            second = run_personal_setup(home, provider="mock", model="local-ready")
            profile = load_profile(home)

            self.assertTrue(first.created)
            self.assertFalse(second.created)
            self.assertEqual(profile.user_id, "local-owner")
            self.assertEqual(profile.hive_id, "personal-hive")
            self.assertEqual(profile.queen_id, "personal-queen")
            self.assertEqual(profile.provider.name, "mock")
            self.assertEqual(profile.provider.model, "local-ready")
            self.assertTrue((home / "memory").is_dir())
            self.assertTrue((home / "runs").is_dir())

    def test_setup_rejects_an_api_key_pasted_as_api_key_env(self) -> None:
        """v48-F2: a literal key in api_key_env corrupted the profile in the
        field — the worker skipped the llm-secret fallback and every run
        failed authentication."""
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp)

            with self.assertRaises(ValueError) as ctx:
                run_personal_setup(
                    home,
                    provider="ollama",
                    model="glm-5.2",
                    endpoint="https://ollama.com",
                    api_key_env="0123456789abcdef.NotARealKeySegment",
                )

            self.assertIn("environment variable NAME", str(ctx.exception))
            self.assertNotIn("NotARealKeySegment", str(ctx.exception))
            self.assertFalse((home / "profile.json").exists())

            # A legit name still round-trips.
            run_personal_setup(
                home,
                provider="ollama",
                model="glm-5.2",
                endpoint="https://ollama.com",
                api_key_env="OLLAMA_API_KEY",
            )
            self.assertEqual(load_profile(home).provider.api_key_env, "OLLAMA_API_KEY")

    def test_doctor_is_read_only_when_profile_is_missing(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp) / "missing-home"

            status = build_status(home)
            report = format_doctor_report(status)

            self.assertEqual(status["overall"], "blocked")
            self.assertFalse(home.exists())
            self.assertIn("run setup", report)
            self.assertNotIn("None", report)

    def test_doctor_advisory_when_only_the_daemon_store_is_configured(self) -> None:
        """v19-F9: profile.json unconfigured but the sqlite store has a provider."""
        from skep.supervisor.serve.llm import LLM_BASE_URL
        from skep.supervisor.store import RunStore

        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp) / "home"
            (home / "supervisor").mkdir(parents=True)
            store = RunStore(home / "supervisor" / "supervisor.sqlite3")
            try:
                store.set_setting(LLM_BASE_URL, "http://provider.example:11434")
            finally:
                store.close()

            status = build_status(home)
            report = format_doctor_report(status)

            self.assertTrue(status["advisories"])
            self.assertIn("supervisor daemon has an LLM provider", report)
            self.assertIn("provider.example", report)

    def test_doctor_memory_check_reads_the_live_store_not_the_retired_path(self) -> None:
        """v111-F2: the one path that drifted. A leftover store at the retired
        ``<home>/supervisor.sqlite3`` layout exists and opens, so the memory
        check spent weeks vouching for a frozen file while serve wrote to
        ``<home>/supervisor/supervisor.sqlite3``."""
        from skep.supervisor.store import RunStore

        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp) / "home"
            (home / "supervisor").mkdir(parents=True)
            # The decoy at the retired layout: exists, opens, holds nothing.
            RunStore(home / "supervisor.sqlite3").close()
            live = RunStore(home / "supervisor" / "supervisor.sqlite3")
            try:
                live.add_memory_item(
                    memory_class="project_fact", content="the live store", actor="test"
                )
            finally:
                live.close()

            memory = build_status(home)["memory"]

            self.assertEqual(memory["path"], str(home / "supervisor" / "supervisor.sqlite3"))
            self.assertEqual(memory["items"], 1)

    def test_doctor_names_projects_that_pin_no_verify_command(self) -> None:
        """v91-F1 (I2/I8): setup pins one for new projects but never rewrites a
        stored policy, so the ones still on the worker-nominated fallback are
        named rather than silently indistinguishable from the pinned ones."""
        from skep.supervisor.store import RunStore

        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp) / "home"
            (home / "supervisor").mkdir(parents=True)
            store = RunStore(home / "supervisor" / "supervisor.sqlite3")
            try:
                for project_id, policy in (
                    ("legacy-proj", {}),
                    ("pinned-proj", {"verify_command": "uv run pytest"}),
                ):
                    store.add_project_policy(
                        project_id=project_id,
                        name=project_id,
                        strategy="trusted_local_dev",
                        phase="maintain",
                        policy=policy,
                    )
            finally:
                store.close()

            report = format_doctor_report(build_status(home))

            self.assertIn("legacy-proj", report)
            self.assertNotIn("pinned-proj", report)
            self.assertIn("verify_command", report)

    def test_doctor_names_umbrella_and_dead_repos(self) -> None:
        """v106-F8: an umbrella binding (a dir containing other registered
        repos) poisons the Queen's shell everywhere under it, and a dead
        binding fails only at dispatch time — doctor names both, and the
        clean project stays unnamed."""
        from skep.supervisor.store import RunStore

        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp) / "home"
            (home / "supervisor").mkdir(parents=True)
            umbrella = Path(tmp) / "developers"
            inner = umbrella / "code" / "inner-proj"
            inner.mkdir(parents=True)
            clean = Path(tmp) / "clean-proj"
            clean.mkdir()
            store = RunStore(home / "supervisor" / "supervisor.sqlite3")
            try:
                for project_id, path in (
                    ("developers", umbrella),
                    ("inner-proj", inner),
                    ("clean-proj", clean),
                    ("dead-proj", Path(tmp) / "gone"),
                ):
                    store.add_project_policy(
                        project_id=project_id,
                        name=project_id,
                        strategy="trusted_local_dev",
                        phase="build",
                        policy={"verify_command": "true"},
                    )
                    store.add_project_binding(
                        project_id=project_id, binding_kind="repo_path", binding_value=str(path)
                    )
            finally:
                store.close()

            report = format_doctor_report(build_status(home))

            self.assertIn("umbrella", report)
            self.assertIn("developers", report)
            self.assertIn("inner-proj", report)
            self.assertIn("shell.deny.repo_cwd", report)
            self.assertIn("dead-proj", report)
            self.assertNotIn("clean-proj (", report)

    def test_doctor_reports_sandbox_even_when_profile_is_missing(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp) / "missing-home"

            with mock.patch(
                "skep.status.sandbox_availability",
                return_value=SandboxAvailability(True, backend="bubblewrap"),
            ):
                status = build_status(home)
            report = format_doctor_report(status)

            self.assertEqual(status["overall"], "blocked")
            self.assertFalse(home.exists())
            self.assertEqual(status["sandbox"]["status"], "ready")
            self.assertIn("sandbox: ready", report)

    def test_missing_provider_credentials_block_readiness_without_printing_secret(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp)
            run_personal_setup(
                home,
                provider="ollama",
                model="minimax-m3:cloud",
                endpoint="http://127.0.0.1:11434",
                api_key_env="SKEP_TEST_API_KEY",
            )

            status = build_status(home, env={})
            report = format_doctor_report(status)

            self.assertEqual(status["overall"], "blocked")
            self.assertIn("SKEP_TEST_API_KEY", report)
            self.assertNotIn("super-secret-value", report)

    def test_invalid_provider_endpoint_blocks_readiness(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp)
            run_personal_setup(
                home,
                provider="ollama",
                model="minimax-m3:cloud",
                endpoint="not-a-url",
            )

            status = build_status(home)

            self.assertEqual(status["overall"], "blocked")
            self.assertEqual(status["required"]["provider"]["status"], "blocked")
            self.assertIn("valid http", status["required"]["provider"]["next_step"])

    def test_valid_provider_endpoint_reports_ready(self) -> None:
        with tempfile.TemporaryDirectory() as tmp, ProviderServer() as endpoint:
            home = Path(tmp)
            run_personal_setup(
                home,
                provider="ollama",
                model="minimax-m3:cloud",
                endpoint=endpoint,
                api_key_env="SKEP_TEST_API_KEY",
            )

            status = build_status(
                home,
                env={"SKEP_TEST_API_KEY": "super-secret-value"},
            )
            report = format_doctor_report(status)

            self.assertEqual(status["overall"], "ready")
            self.assertEqual(status["required"]["provider"]["status"], "ready")
            self.assertNotIn("super-secret-value", json.dumps(status))
            self.assertNotIn("super-secret-value", report)

    def test_worker_check_blocks_when_the_credential_env_var_is_unset(self) -> None:
        """v49-F1: the worker resolves credentials on its OWN path (api_key_env
        wins over the llm-secret fallback); doctor now walks that exact path,
        catching a broken credential before any run fails on it."""
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp)
            run_personal_setup(
                home,
                provider="ollama",
                model="glm-5.2",
                endpoint="https://ollama.example",
                api_key_env="SKEP_UNSET_CREDENTIAL",
            )

            status = build_status(home, env={})
            worker = status["workers"]["coding_worker"]

            self.assertEqual(worker["status"], "blocked")
            self.assertIn("SKEP_UNSET_CREDENTIAL", worker["detail"])

    def test_worker_check_probes_the_daemon_settings_fallback(self) -> None:
        """A mock profile with daemon LLM settings resolves the worker provider
        from the store (v8 bootstrap path) and probes it live."""
        from skep.supervisor.serve.llm import LLM_BASE_URL, LLM_DEFAULT_MODEL
        from skep.supervisor.store import RunStore

        with tempfile.TemporaryDirectory() as tmp, ProviderServer() as endpoint:
            home = Path(tmp)
            run_personal_setup(home, provider="mock", model="local-ready")
            (home / "supervisor").mkdir(exist_ok=True)
            store = RunStore(home / "supervisor" / "supervisor.sqlite3")
            try:
                store.set_setting(LLM_BASE_URL, endpoint)
                store.set_setting(LLM_DEFAULT_MODEL, "qwen3")
            finally:
                store.close()

            status = build_status(home)
            worker = status["workers"]["coding_worker"]

            self.assertEqual(worker["status"], "ready")
            self.assertIn("qwen3", worker["label"])
            self.assertIn(endpoint, worker["detail"])

    def test_dashboard_renders_the_same_status_categories_as_doctor(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp)
            run_personal_setup(home, provider="mock", model="local-ready")
            status = build_status(home)

            html = render_dashboard(status)
            report = format_doctor_report(status)

            self.assertIn("ready", html)
            self.assertIn("mock", html)
            self.assertIn("coding_worker", html)
            # v49-F1: the worker check is honest now — a mock profile with no
            # daemon settings has NO worker provider, and doctor says so.
            self.assertIn("no worker provider configured", html)
            self.assertIn("coding_worker", report)
            self.assertIn("no worker provider configured", report)

    def test_dashboard_does_not_show_coding_worker_as_planned_after_bridge_landed(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp)
            run_personal_setup(home, provider="mock", model="local-ready")
            status = build_status(home)

            html = render_dashboard(status)

            self.assertNotIn("Not connected yet", html)
            self.assertNotIn("Coding-worker supervision arrives", html)

    def test_doctor_reports_usable_sandbox_backend(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp)
            run_personal_setup(home, provider="mock", model="local-ready")

            with mock.patch(
                "skep.status.sandbox_availability",
                return_value=SandboxAvailability(True, backend="bubblewrap"),
            ):
                status = build_status(home)
            report = format_doctor_report(status)

            self.assertEqual(status["overall"], "ready")
            self.assertEqual(status["sandbox"]["status"], "ready")
            self.assertEqual(status["sandbox"]["backend"], "bubblewrap")
            self.assertIn("sandbox: ready", report)
            self.assertIn("bubblewrap", report)

    def test_unavailable_sandbox_is_reported_without_blocking_workspace_mode(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp)
            run_personal_setup(home, provider="mock", model="local-ready")

            with mock.patch(
                "skep.status.sandbox_availability",
                return_value=SandboxAvailability(
                    False,
                    reason="missing_binary",
                    detail="bwrap was not found on PATH",
                ),
            ):
                status = build_status(home)
            report = format_doctor_report(status)

            self.assertEqual(status["overall"], "ready")
            self.assertEqual(status["sandbox"]["status"], "unavailable")
            self.assertIn("missing_binary", report)
            self.assertIn("bwrap was not found on PATH", report)


if __name__ == "__main__":
    unittest.main()
