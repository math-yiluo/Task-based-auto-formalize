import asyncio
import json
from types import SimpleNamespace

from typer.testing import CliRunner

from formal_geyser.coordination.cli import app
from formal_geyser.coordination.config import initialize
from formal_geyser.coordination.hub import Hub
from formal_geyser.coordination.models import Actor, Command, ProjectConfig, Role

runner = CliRunner()


def test_init_preserves_existing_files_and_refuses_to_overwrite_project(tmp_path):
    note = tmp_path / "notes.txt"
    note.write_text("Keep this record", encoding="utf-8")
    result = runner.invoke(app, ["init", str(tmp_path)])
    assert result.exit_code == 0, result.output
    config = (tmp_path / "project.toml").read_bytes()
    assert note.read_text(encoding="utf-8") == "Keep this record"
    repeat = runner.invoke(app, ["init", str(tmp_path), "--name", "Replacement"])
    assert repeat.exit_code == 1
    assert (tmp_path / "project.toml").read_bytes() == config
    assert not (tmp_path / ".task-based" / "operator-token").exists()


def test_workers_import_history_sends_the_titles_in_order(tmp_path, monkeypatch):
    calls = []

    def capture(ctx, name, payload, **kwargs):
        calls.append((name, payload))
        return {"worker_id": payload["worker_id"], "tasks": payload["tasks"]}

    monkeypatch.setattr("formal_geyser.coordination.cli._command", capture)
    result = runner.invoke(app, ["--project", str(tmp_path), "workers", "import-history", "Worker 27",
                                 "--task", "Newest task", "--task", "Older task"])
    assert result.exit_code == 0, result.output
    assert calls == [("worker.import_history", {"worker_id": "Worker 27", "tasks": ["Newest task", "Older task"]})]


def test_run_start_has_no_global_concurrency_limit(tmp_path, monkeypatch):
    calls = []

    def capture(ctx, name, payload, **kwargs):
        calls.append((name, payload, kwargs))
        return {"id": "new-run"}

    monkeypatch.setattr("formal_geyser.coordination.cli._command", capture)
    result = runner.invoke(app, ["--project", str(tmp_path), "run", "start"])
    assert result.exit_code == 0, result.output
    assert calls == [("run.start", {},
                     {"key": None, "drive": True})]


def test_doctor_probes_version_without_model_work(tmp_path, monkeypatch):
    initialize(tmp_path, ProjectConfig())
    calls = []
    monkeypatch.setattr("formal_geyser.cgroups.root", lambda: tmp_path / "owned")
    monkeypatch.setattr("formal_geyser.coordination.native_permissions.wsl_interop", lambda: False)
    monkeypatch.setattr("formal_geyser.coordination.runtime.resolve_codex_executable", lambda name: "/usr/bin/codex")

    def version(arguments, **kwargs):
        calls.append(arguments)
        return SimpleNamespace(returncode=0, stdout="codex-cli 0.160.0\n", stderr="")

    monkeypatch.setattr("formal_geyser.coordination.cli.subprocess.run", version)
    result = runner.invoke(app, ["--project", str(tmp_path), "doctor"])
    assert result.exit_code == 0, result.output
    assert calls == [["/usr/bin/codex", "--version"]]
    report = json.loads(result.stdout)
    assert report["ok"] is True and report["process_ownership"] == {"ok": True, "cgroup": str(tmp_path / "owned")}


def test_doctor_reports_wsl_interop_as_a_sandbox_failure(tmp_path, monkeypatch):
    initialize(tmp_path, ProjectConfig())
    monkeypatch.setattr("formal_geyser.cgroups.root", lambda: tmp_path / "owned")
    monkeypatch.setattr("formal_geyser.coordination.native_permissions.wsl_interop", lambda: True)
    result = runner.invoke(app, ["--project", str(tmp_path), "doctor"])
    assert result.exit_code == 1 and "interop" in json.loads(result.stdout)["sandbox"]["message"]


def test_remote_url_cannot_expose_operator_token(tmp_path):
    initialize(tmp_path, ProjectConfig())
    result = runner.invoke(
        app, ["--project", str(tmp_path), "--url", "https://example.com", "tasks", "list"]
    )
    assert result.exit_code == 2
    assert "loopback" in result.output


def test_dashboard_prints_the_sign_in_link_once_the_service_has_a_token(tmp_path):
    from formal_geyser.coordination.local_operator import issue_token

    initialize(tmp_path, ProjectConfig())
    refused = runner.invoke(app, ["--project", str(tmp_path), "dashboard"])
    assert refused.exit_code == 1 and "taskctl serve" in refused.output
    token = issue_token(tmp_path, "taskhub")
    shown = runner.invoke(app, ["--project", str(tmp_path), "dashboard", "--port", "9000"])
    assert shown.exit_code == 0 and shown.stdout.strip() == f"http://127.0.0.1:9000/login?token={token}"


def test_cli_can_publish_query_and_backup_without_runtime(tmp_path):
    initialize(tmp_path, ProjectConfig())
    contract = tmp_path / "contract.json"
    contract.write_text(
        json.dumps({"title": "Review a record", "objective": "Review the provided record"}),
        encoding="utf-8",
    )
    prefix = ["--project", str(tmp_path)]
    published = runner.invoke(app, [*prefix, "tasks", "publish", str(contract)])
    assert published.exit_code == 0, published.output
    task_id = json.loads(published.stdout)["id"]
    listed = runner.invoke(app, [*prefix, "tasks", "list"])
    assert listed.exit_code == 0, listed.output
    assert json.loads(listed.stdout)[0]["id"] == task_id
    backup = tmp_path / "backups" / "first"
    backed_up = runner.invoke(app, [*prefix, "backup", str(backup)])
    assert backed_up.exit_code == 0, backed_up.output
    assert (backup / "project.toml").exists()
    restored = runner.invoke(app, ["--project", str(backup), "tasks", "list"])
    assert restored.exit_code == 0, restored.output
    assert json.loads(restored.stdout)[0]["id"] == task_id
    repeated = runner.invoke(app, [*prefix, "backup", str(backup)])
    assert repeated.exit_code == 1


def test_serve_refuses_non_loopback_bind_address(tmp_path):
    initialize(tmp_path, ProjectConfig())
    result = runner.invoke(
        app, ["--project", str(tmp_path), "serve", "--host", "0.0.0.0"]
    )
    assert result.exit_code == 1
    assert "loopback" in result.output


def test_publish_cannot_import_a_domain_other_than_the_project_domain(tmp_path, monkeypatch):
    initialize(tmp_path, ProjectConfig())
    contract = tmp_path / "foreign.json"
    contract.write_text(json.dumps({
        "title": "Review record", "objective": "Review this record", "domain": "foreign"
    }), encoding="utf-8")

    def fail(domain):
        raise AssertionError(f"Unconfigured domain was imported: {domain}")

    monkeypatch.setattr("formal_geyser.coordination.cli.load_plugin", fail)
    result = runner.invoke(app, [
        "--project", str(tmp_path), "tasks", "publish", str(contract)
    ])
    assert result.exit_code == 1
    assert "does not match project domain" in result.output


def test_explicit_operator_review_creates_unqualified_acceptance_receipt(tmp_path):
    initialize(tmp_path, ProjectConfig())

    async def register():
        hub = Hub(tmp_path)
        try:
            return await hub.execute(
                Command(name="artifact.register", key="record", payload={
                    "kind": "general.record", "data": {"ready": True},
                }),
                Actor(role=Role.OPERATOR),
            )
        finally:
            await hub.close()

    artifact = asyncio.run(register())
    result = runner.invoke(app, [
        "--project", str(tmp_path), "artifacts", "accept", artifact["id"],
        "--policy", "general.manual@1", "--reason", "Reviewed the supplied record",
    ])
    assert result.exit_code == 0, result.output
    record = json.loads(result.stdout)
    assert record["accepted"] is True
    assert record["evidence"][-1]["qualifications"] == []
    assert record["evidence"][-1]["details"]["operator_review"] == "Reviewed the supplied record"


def test_worker_recovery_requires_explicit_stopped_confirmation_and_reason(monkeypatch):
    calls = []

    def capture(ctx, name, payload, **kwargs):
        calls.append((name, payload, kwargs))
        return {"state": "recovered"}

    monkeypatch.setattr("formal_geyser.coordination.cli._command", capture)
    missing = runner.invoke(app, [
        "workers", "recover", "worker-1", "--reason", "Checked owned processes"
    ])
    assert missing.exit_code == 2
    assert "confirm-stopped" in missing.output
    assert calls == []
    blank = runner.invoke(app, [
        "workers", "recover", "worker-1", "--confirm-stopped", "--reason", " "
    ])
    assert blank.exit_code == 2
    assert calls == []
    confirmed = runner.invoke(app, [
        "workers", "recover", "worker-1", "--confirm-stopped",
        "--reason", "Confirmed that all previously owned processes stopped",
    ])
    assert confirmed.exit_code == 0, confirmed.output
    assert calls == [("worker.recover", {
        "worker_id": "worker-1", "process_confirmed_stopped": True,
        "reason": "Confirmed that all previously owned processes stopped",
    }, {})]


def test_usage_reconciliation_records_evidence_without_starting_a_driver(monkeypatch):
    calls = []

    def capture(ctx, name, payload, **kwargs):
        calls.append((name, payload, kwargs))
        return {"state": "paused", "usage_uncertain": 0}

    monkeypatch.setattr("formal_geyser.coordination.cli._command", capture)
    negative = runner.invoke(app, [
        "run", "reconcile-usage", "run-1", "--tokens-used", "-1", "--reason", "Reviewed"
    ])
    assert negative.exit_code == 2
    assert calls == []
    blank = runner.invoke(app, [
        "run", "reconcile-usage", "run-1", "--tokens-used", "125", "--reason", " "
    ])
    assert blank.exit_code == 2
    assert calls == []
    reconciled = runner.invoke(app, [
        "run", "reconcile-usage", "run-1", "--tokens-used", "125",
        "--reason", "Checked the provider usage record",
    ])
    assert reconciled.exit_code == 0, reconciled.output
    assert calls == [("run.reconcile_usage", {
        "run_id": "run-1", "tokens_used": 125, "reason": "Checked the provider usage record",
        "backend": "codex",
    }, {})]
    # The reconciled tokens are charged to the named account.
    claude = runner.invoke(app, [
        "run", "reconcile-usage", "run-1", "--tokens-used", "130",
        "--reason", "Checked the Claude usage page", "--backend", "claude",
    ])
    assert claude.exit_code == 0, claude.output
    assert calls[-1][1]["backend"] == "claude"


def test_cli_recovers_crash_hold_and_reconciles_usage_without_automatic_execution(
    tmp_path, monkeypatch
):
    initialize(tmp_path, ProjectConfig())
    checkpoint = {"summary": "Saved software review", "next_action": "Inspect saved progress"}

    async def prepare_interrupted_attempt():
        hub = Hub(tmp_path)

        async def command(name, payload, actor=Actor(role=Role.OPERATOR)):
            return await hub.execute(Command(name=name, key=name, payload=payload), actor)

        try:
            task = await command("task.publish", {"contract": {
                "title": "Review a record", "objective": "Review the provided software record",
            }})
            run = await command("run.start", {})
            worker = await command("worker.register", {})
            attempt = await command("task.claim", {
                "task_id": task["id"], "revision": task["revision"],
                "worker_id": worker["id"], "run_id": run["id"],
            })
            actor = Actor(
                role=Role.WORKER, worker_id=worker["id"],
                attempt_id=attempt["id"], fencing_token=attempt["fencing_token"],
            )
            await command("task.checkpoint", {"checkpoint": checkpoint}, actor)
            (hub.workspace(worker["id"]) / "progress.txt").write_text(
                "Durable software progress", encoding="utf-8"
            )
            intent = await command(
                "turn.reserve", {"worker_id": worker["id"]}, Actor(role=Role.RUNNER)
            )
            await command("turn.bind", {
                "intent_id": intent["id"], "remote_turn_id": "fixture-turn",
            }, Actor(role=Role.RUNNER))
            return worker["id"], run["id"]
        finally:
            await hub.close()

    worker_id, run_id = asyncio.run(prepare_interrupted_attempt())

    def forbidden_driver(*args, **kwargs):
        raise AssertionError("Recovery and reconciliation must not execute model work")

    monkeypatch.setattr("formal_geyser.coordination.cli._drive", forbidden_driver)
    prefix = ["--project", str(tmp_path)]
    inspection = runner.invoke(app, [*prefix, "workers", "show", worker_id])
    assert inspection.exit_code == 0, inspection.output
    assert json.loads(inspection.stdout)["state"] == "recovery_required"
    recovered = runner.invoke(app, [
        *prefix, "workers", "recover", worker_id, "--confirm-stopped",
        "--reason", "The fixture has no external process",
    ])
    assert recovered.exit_code == 0, recovered.output
    recovered_record = json.loads(recovered.stdout)
    assert recovered_record["checkpoint"]["summary"] == checkpoint["summary"]
    assert len(recovered_record["snapshot"]) == 64
    assert recovered_record["directive"] == "recovered"
    assert not recovered_record.get("terminal_reconciliation_required")
    reconciled = runner.invoke(app, [
        *prefix, "run", "reconcile-usage", run_id,
        "--tokens-used", "125", "--reason", "Reviewed the synthetic usage record",
    ])
    assert reconciled.exit_code == 0, reconciled.output
    state = json.loads(reconciled.stdout)
    assert state["tokens_used"] == 125
    assert state["usage_uncertain"] == 0
    assert state["state"] == "running"
    repeat = runner.invoke(app, [
        *prefix, "workers", "recover", worker_id, "--confirm-stopped",
        "--reason", "The fixture has no external process",
    ])
    assert repeat.exit_code == 1
    # The stopped invocation already released execution; accounting is separate.
    assert "RECOVERY_NOT_REQUIRED" in repeat.output


def test_cli_run_start_rejects_retired_global_limits(tmp_path, monkeypatch):
    calls = []

    def capture(ctx, name, payload, **kwargs):
        calls.append((name, payload, kwargs))
        return {"id": "new-run"}

    monkeypatch.setattr("formal_geyser.coordination.cli._command", capture)
    result = runner.invoke(app, [
        "--project", str(tmp_path), "run", "start",
    ])
    assert result.exit_code == 0, result.output
    assert calls == [("run.start", {},
                     {"key": None, "drive": True})]
    calls.clear()
    for arguments in (["--max-active", "3"], ["--max-turns", "3"], ["--token-allowance", "3"], ["--unlimited"]):
        result = runner.invoke(app, [
            "--project", str(tmp_path), "run", "start", *arguments,
        ])
        assert result.exit_code == 2
        assert "No such option" in result.output
    assert calls == []


def test_repository_commands_preserve_operator_scope_without_driving_workers(tmp_path, monkeypatch):
    calls = []

    def capture(ctx, name, payload, **kwargs):
        calls.append((name, payload, kwargs))
        return {"head": "commit-1", "branch": "main"}

    monkeypatch.setattr("formal_geyser.coordination.cli._command", capture)
    initialized = runner.invoke(app, [
        "--project", str(tmp_path), "repository", "initialize", "--snapshot", "a" * 64,
        "--key", "repository-init",
    ])
    assert initialized.exit_code == 0, initialized.output
    assert calls == [("repository.initialize", {"snapshot": "a" * 64},
                      {"key": "repository-init"})]
    retired = runner.invoke(app, ["repository", "reconcile", "integration-1", "--confirm-stopped",
                                  "--reason", "Serial reviewer merges no longer exist."])
    assert retired.exit_code == 2 and len(calls) == 1


def test_repository_status_and_initialization_use_service_when_selected(tmp_path, monkeypatch):
    calls = []

    def capture(settings, method, path, **kwargs):
        calls.append((method, path, kwargs))
        return {"enabled": True, "head": None}

    monkeypatch.setattr("formal_geyser.coordination.cli._request", capture)
    prefix = ["--project", str(tmp_path), "--url", "http://127.0.0.1:8765", "repository"]
    status = runner.invoke(app, [*prefix, "status"])
    assert status.exit_code == 0, status.output
    assert calls == [("GET", "/v1/repository", {"params": {}})]
    initialized = runner.invoke(app, [*prefix, "initialize", "--key", "empty-repository"])
    assert initialized.exit_code == 0, initialized.output
    assert calls[-1] == ("POST", "/v1/commands", {"json": {
        "name": "repository.initialize", "key": "empty-repository", "payload": {},
    }})
    assert len(calls) == 2


def test_init_repository_flag_enables_explicit_setup_without_initializing_git(tmp_path, monkeypatch):
    from formal_geyser.coordination.config import load_config

    def forbid_driver(*args, **kwargs):
        raise AssertionError("Project creation must not start workers")

    monkeypatch.setattr("formal_geyser.coordination.cli._drive", forbid_driver)
    result = runner.invoke(app, ["init", str(tmp_path), "--repository"])
    assert result.exit_code == 0, result.output
    assert load_config(tmp_path).repository.enabled
    assert not (tmp_path / ".task-based" / "repository.git").exists()
    setup = json.loads(result.stdout)["repository"]
    assert setup["enabled"] and not setup["initialized"]
    assert "repository initialize" in setup["next_action"]


def test_repository_initialize_refuses_disabled_configuration_without_starting_workers(tmp_path, monkeypatch):
    initialize(tmp_path, ProjectConfig())

    def forbid_driver(*args, **kwargs):
        raise AssertionError("Repository initialization must not start workers")

    monkeypatch.setattr("formal_geyser.coordination.cli._drive", forbid_driver)
    result = runner.invoke(app, ["--project", str(tmp_path), "repository", "initialize"])
    assert result.exit_code == 1
    assert "REPOSITORY_DISABLED" in result.output
    assert not (tmp_path / ".task-based" / "repository.git").exists()
    status = runner.invoke(app, ["--project", str(tmp_path), "repository", "status"])
    assert status.exit_code == 0, status.output
    assert json.loads(status.stdout) == {"enabled": False}
