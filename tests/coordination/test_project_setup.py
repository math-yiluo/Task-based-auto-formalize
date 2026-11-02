"""Project setup boundaries using synthetic catalogs and no native model execution."""
from __future__ import annotations

import asyncio
import json
import shutil
from contextlib import asynccontextmanager
from copy import deepcopy
from pathlib import Path
from types import SimpleNamespace
from uuid import uuid4

import httpx
import pytest

from formal_geyser.coordination.api import create_app
from formal_geyser.coordination.config import initialize, load_config
from formal_geyser.coordination.errors import HubError
from formal_geyser.coordination.models import (
    CodexConfig,
    ProjectConfig,
    RepositoryConfig,
)

pytestmark = pytest.mark.asyncio

CATALOG = {
    "models": [
        {
            "id": "synthetic-default",
            "name": "Synthetic default",
            "description": "Metadata fixture; never executes a real model.",
            "efforts": [{"id": "low", "description": "Low"},
                        {"id": "high", "description": "High"}],
            "default_effort": "high",
            "is_default": True,
        },
        {
            "id": "synthetic-selected",
            "name": "Synthetic selected",
            "description": "A deliberately different runtime profile.",
            "efforts": [{"id": "minimal", "description": "Minimal"},
                        {"id": "xhigh", "description": "Extra high"}],
            "default_effort": "minimal",
            "is_default": False,
        },
    ],
    "authentication_required": False,
    "source": "synthetic-test-catalog",
}


class Catalog:
    def __init__(self):
        self.value = deepcopy(CATALOG)
        self.calls = []
        self.error = None

    async def __call__(self, config, root):
        self.calls.append((deepcopy(config), root))
        if self.error is not None:
            raise self.error
        return deepcopy(self.value)


class RunnerProbe:
    """Keep the driver alive at a controlled boundary without creating a worker."""

    def __init__(self):
        self.profiles = []
        self.saved_profiles = []
        self.runs = []
        self.started = asyncio.Event()
        self.release = asyncio.Event()

    def __call__(self, hub):
        self.profiles.append(hub.config.codex.model_dump(mode="json"))
        self.saved_profiles.append(load_config(hub.root).codex.model_dump(mode="json"))
        return self

    async def run_until_idle(self, run_id, **_):
        self.runs.append(run_id)
        self.started.set()
        await self.release.wait()
        return {"state": "synthetic_idle"}


@pytest.fixture
def project_factory(tmp_path_factory):
    def create(domain="general"):
        root = tmp_path_factory.mktemp("ps")
        initialize(root, ProjectConfig(
            name="Synthetic setup project", domain=domain,
            codex=CodexConfig(
                home=str(root / "synthetic-home"), executable="synthetic-codex-never-run",
                model="synthetic-original", effort="high", stop_grace_seconds=3.0,
            ),
            repository=RepositoryConfig(branch="setup-tests"),
        ))
        return root
    return create


@pytest.fixture
def project(project_factory):
    return project_factory()


def request(**changes):
    return {
        "key": uuid4().hex,
        "goal": "Inspect the supplied synthetic software record and document the result.",
        "model": "synthetic-selected",
        "effort": "xhigh",
        "task_id": None,
        "task_revision": None,
        **changes,
    }


@asynccontextmanager
async def service(root, *, catalog=None, probe=None, **kwargs):
    catalog = catalog or Catalog()
    probe = probe or RunnerProbe()
    app = create_app(root, runner_factory=probe, model_catalog_loader=catalog, **kwargs)
    async with app.router.lifespan_context(app):
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app, raise_app_exceptions=False),
            base_url="http://testserver",
        ) as client:
            yield SimpleNamespace(root=root, app=app, client=client, catalog=catalog, probe=probe)


async def command(case, name, payload=None):
    response = await case.client.post("/v1/commands", json={
        "name": name, "key": uuid4().hex, "payload": payload or {},
    })
    assert response.status_code == 200, response.text
    return response.json()


async def start(case, body):
    response = await case.client.post("/v1/setup/start", json=body)
    assert response.status_code in {200, 201, 202}, response.text
    return response.json()


async def setup_status(case):
    response = await case.client.get("/v1/setup")
    assert response.status_code == 200, response.text
    return response.json()


async def pristine(case, config_bytes):
    assert (case.root / "project.toml").read_bytes() == config_bytes
    assert await case.app.state.hub.list_tasks() == []
    assert await case.app.state.hub.list_workers() == []
    assert await case.app.state.hub.list_runs() == []
    assert case.app.state.driver.armed == set()
    assert case.app.state.driver.passes == {}
    assert case.probe.profiles == []
    assert case.probe.runs == []
    status = await setup_status(case)
    assert status["can_configure"] is True
    assert status["task"] is None and status["run"] is None
    assert status["receipt"] is None and status["can_drive"] is False
    assert status["repository"] == {"enabled": False, "initialized": False}


async def finish_driver(case, run_id):
    # Periodic passes are covered by test_api; this isolates request/replay demand.
    case.app.state.driver.timer_task.cancel()
    await asyncio.gather(case.app.state.driver.timer_task, return_exceptions=True)
    case.probe.release.set()
    await asyncio.wait_for(asyncio.shield(case.app.state.driver.passes[run_id]), timeout=10)


def require_git():
    if shutil.which("git") is None:
        pytest.skip("Local Git is required for the managed repository")


async def test_setup_endpoints_refuse_non_operators_before_loading_models(project, monkeypatch):
    saved = (project / "project.toml").read_bytes()
    async with service(project) as case:
        monkeypatch.setattr("formal_geyser.coordination.api.operator_request", lambda request, token: False)
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=case.app), base_url="http://testserver",
        ) as anonymous:
            assert (await anonymous.get("/v1/setup")).status_code == 401
            assert (await anonymous.get("/v1/setup/models")).status_code == 401
            assert (await anonymous.post("/v1/setup/start", json=request())).status_code == 401
        assert case.catalog.calls == []
        monkeypatch.setattr("formal_geyser.coordination.api.operator_request", lambda request, token: True)
        await pristine(case, saved)


async def test_setup_status_is_read_only_and_does_not_discover_models(project):
    saved = (project / "project.toml").read_bytes()
    async with service(project) as case:
        events = await case.app.state.hub.events()
        first = await setup_status(case)
        assert first["project_id"]
        assert first["settings"] == {"model": "synthetic-original", "effort": "high"}
        for _ in range(3):
            assert await setup_status(case) == first
        assert case.catalog.calls == []
        assert await case.app.state.hub.events() == events
        await pristine(case, saved)
    async with service(project) as restarted:
        assert (await setup_status(restarted))["project_id"] == first["project_id"]
        assert restarted.catalog.calls == []
        await pristine(restarted, saved)


async def test_setup_catalog_uses_current_config_without_starting_work(project):
    saved = (project / "project.toml").read_bytes()
    async with service(project) as case:
        response = await case.client.get("/v1/setup/models")
        assert response.status_code == 200, response.text
        assert response.json() == CATALOG
        assert len(case.catalog.calls) == 1
        config, catalog_root = case.catalog.calls[0]
        assert config == load_config(project).codex
        assert catalog_root == project.resolve()
        await pristine(case, saved)
        assert len(case.catalog.calls) == 1


async def test_matching_project_id_starts_and_replays_the_original_receipt(project):
    require_git()
    async with service(project) as case:
        project_id = (await setup_status(case))["project_id"]
        body = request(project_id=project_id)
        first = await start(case, body)
        await asyncio.wait_for(case.probe.started.wait(), timeout=10)
        calls = len(case.catalog.calls)
        repeated = await start(case, body)
        assert first["replayed"] is False and repeated["replayed"] is True
        assert repeated["task_id"] == first["task_id"]
        assert repeated["run_id"] == first["run_id"]
        assert (await setup_status(case))["project_id"] == project_id
        assert len(case.catalog.calls) == calls
        assert case.probe.runs == [first["run_id"]]
        assert case.app.state.driver.pending_wakes == set()
        assert len(await case.app.state.hub.list_tasks()) == 1
        assert len(await case.app.state.hub.list_runs()) == 1


@pytest.mark.parametrize("replay", [False, True], ids=["new-start", "existing-receipt"])
async def test_mismatched_project_id_rejects_before_catalog_or_mutation(project, replay):
    if replay:
        require_git()
    async with service(project) as case:
        body = request(project_id=(await setup_status(case))["project_id"])
        if replay:
            await start(case, body)
            await asyncio.wait_for(case.probe.started.wait(), timeout=10)
        saved = (project / "project.toml").read_bytes()
        status = await setup_status(case)
        tasks = await case.app.state.hub.list_tasks()
        runs = await case.app.state.hub.list_runs()
        events = await case.app.state.hub.events()
        calls = len(case.catalog.calls)
        activations = list(case.probe.runs)
        case.catalog.error = HubError("catalog_unavailable", "Catalog must not be consulted", 503)
        response = await case.client.post("/v1/setup/start", json={
            **body, "project_id": "a-different-synthetic-project",
        })
        assert response.status_code == 409, response.text
        assert response.json()["error"]["code"] == "SETUP_PROJECT_CHANGED"
        assert len(case.catalog.calls) == calls
        assert (project / "project.toml").read_bytes() == saved
        assert await case.app.state.hub.list_tasks() == tasks
        assert await case.app.state.hub.list_runs() == runs
        assert await case.app.state.hub.events() == events
        assert await setup_status(case) == status
        assert case.probe.runs == activations
        assert case.app.state.driver.pending_wakes == set()
        if not replay:
            await pristine(case, saved)


@pytest.mark.parametrize("project_id", ["", True, "p" * 65],
                         ids=["empty", "boolean", "oversize"])
async def test_invalid_project_id_is_rejected_before_catalog_or_mutation(project, project_id):
    saved = (project / "project.toml").read_bytes()
    async with service(project) as case:
        response = await case.client.post("/v1/setup/start", json=request(project_id=project_id))
        assert response.status_code == 422, response.text
        assert case.catalog.calls == []
        await pristine(case, saved)


@pytest.mark.parametrize("domain,policy", [
    ("general", "general.peer@1"), ("lean", "lean.peer@1"),
])
async def test_start_persists_selected_profile_and_native_task_zero(project_factory, domain, policy):
    require_git()
    project = project_factory(domain)
    original = load_config(project)
    goal = "  # Software fixture goal\r\nInspect the synthetic record.\nPreserve exact wording.  \n"
    body = request(goal=goal)
    async with service(project) as case:
        if domain == "lean":
            from tests.bootstrap_helpers import synthetic_execution
            synthetic_execution(case.app.state.hub)
        result = await start(case, body)
        await asyncio.wait_for(case.probe.started.wait(), timeout=10)
        assert result["replayed"] is False
        assert result["armed"] is True and result["driving"] is True
        assert case.probe.runs == [result["run_id"]]
        expected_profile = original.codex.model_copy(update={
            "model": body["model"], "effort": body["effort"],
        }).model_dump(mode="json")
        assert case.probe.profiles == [expected_profile]
        assert case.probe.saved_profiles == [expected_profile]
        persisted = load_config(project)
        assert persisted.codex.model_dump(mode="json") == expected_profile
        assert persisted.model_dump(exclude={"codex", "repository"}) == original.model_dump(
            exclude={"codex", "repository"})
        assert persisted.repository == original.repository.model_copy(update={"enabled": True})

        tasks = await case.app.state.hub.list_tasks()
        runs = await case.app.state.hub.list_runs()
        assert [task["id"] for task in tasks] == [result["task_id"]]
        assert [run["id"] for run in runs] == [result["run_id"]]
        task = tasks[0]
        assert task["revision"] == 1
        assert task["contract"]["objective"] == goal
        assert task["contract"]["domain"] == domain
        assert task["contract"]["acceptance_policy"] == policy
        assert task["state"] == "ready"
        native_root = Path(task["files"]["root"])
        assert (native_root / "objective.md").read_bytes() == goal.encode("utf-8")
        native_task = json.loads((native_root / "task.json").read_bytes())
        assert native_task["objective"] == "objective.md"
        assert "objective" not in native_task["contract"]
        lifecycle = await case.app.state.hub.db.call(lambda c: dict(c.execute(
            "SELECT * FROM project_lifecycle WHERE id=1").fetchone()))
        assert lifecycle["root_task_id"] == result["task_id"]
        assert lifecycle["state"] == "running"

        repository = (await case.client.get("/v1/repository")).json()
        assert repository["enabled"] is True and repository["initialized"] is True
        assert repository["reconciliation_required"] is False
        assert repository["branch"] == "setup-tests"
        status = await setup_status(case)
        assert status["can_configure"] is False
        assert status["can_drive"] is False
        assert status["settings"] == {"model": body["model"], "effort": body["effort"]}
        assert status["receipt"] == {key: value for key, value in {
            **body, **result,
        }.items() if key in {"key", "task_id", "run_id", "model", "effort"}}
        assert await case.app.state.hub.list_workers() == []


async def test_start_retry_replays_exact_ids_without_new_catalog_or_pending_drive(project):
    require_git()
    body = request()
    async with service(project) as case:
        first = await start(case, body)
        await asyncio.wait_for(case.probe.started.wait(), timeout=10)
        saved = (project / "project.toml").read_bytes()
        calls = len(case.catalog.calls)
        case.catalog.error = HubError("unavailable", "Synthetic catalog is now offline", 503)
        repeated = await start(case, body)
        assert repeated["replayed"] is True
        assert repeated["task_id"] == first["task_id"]
        assert repeated["run_id"] == first["run_id"]
        assert len(case.catalog.calls) == calls
        assert case.app.state.driver.pending_wakes == set()
        await finish_driver(case, first["run_id"])
        assert case.probe.runs == [first["run_id"]]
        idle_retry = await start(case, body)
        assert idle_retry["replayed"] is True
        assert idle_retry["armed"] is True and idle_retry["driving"] is False
        assert case.app.state.driver.pending_wakes == set()
        assert case.probe.runs == [first["run_id"]]
        assert (project / "project.toml").read_bytes() == saved
        assert len(await case.app.state.hub.list_tasks()) == 1
        assert len(await case.app.state.hub.list_runs()) == 1


@pytest.mark.parametrize("same_key", [True, False], ids=["double-click", "competing-keys"])
async def test_concurrent_starts_create_one_task_run_and_activation(project, same_key):
    require_git()
    first = request()
    second = dict(first) if same_key else request(goal="Another synthetic software goal.")
    async with service(project) as case:
        responses = await asyncio.gather(
            case.client.post("/v1/setup/start", json=first),
            case.client.post("/v1/setup/start", json=second),
        )
        successes = [item.json() for item in responses if item.status_code in {200, 201, 202}]
        assert len(successes) == (2 if same_key else 1), [item.text for item in responses]
        if same_key:
            assert sorted(item["replayed"] for item in successes) == [False, True]
            assert len({item["task_id"] for item in successes}) == 1
            assert len({item["run_id"] for item in successes}) == 1
        else:
            assert sorted(item.status_code == 409 for item in responses) == [False, True]
        await asyncio.wait_for(case.probe.started.wait(), timeout=10)
        assert case.probe.runs == [successes[0]["run_id"]]
        assert case.app.state.driver.pending_wakes == set()
        assert len(await case.app.state.hub.list_tasks()) == 1
        assert len(await case.app.state.hub.list_runs()) == 1


@pytest.mark.parametrize("change", [
    {"goal": "A conflicting synthetic goal."},
    {"model": "synthetic-default", "effort": "low"},
    {"key": "another-start-key"},
])
async def test_start_cannot_reconfigure_an_existing_receipt(project, change):
    require_git()
    body = request()
    async with service(project) as case:
        result = await start(case, body)
        await asyncio.wait_for(case.probe.started.wait(), timeout=10)
        saved = (project / "project.toml").read_bytes()
        tasks = await case.app.state.hub.list_tasks()
        response = await case.client.post("/v1/setup/start", json={**body, **change})
        assert response.status_code == 409, response.text
        assert (project / "project.toml").read_bytes() == saved
        assert await case.app.state.hub.list_tasks() == tasks
        assert case.probe.runs == [result["run_id"]]
        assert case.app.state.driver.pending_wakes == set()


async def publish_root(case):
    original = await command(case, "task.publish", {"contract": {
        "title": "Existing Task 0", "objective": "Original exact software objective.\r\n",
        "context": "Retain this original context.\n", "constraints": ["Retain the contract."],
        "priority": 3, "acceptance_policy": "general.peer@1",
        "payload": {"fixture": ["unchanged", 7]},
    }})
    return await command(case, "task.revise", {
        "task_id": original["id"], "revision": 1, "reason": "Publish the final fixture wording.",
        "contract": {**original["contract"], "objective": "# Published goal\r\nExact revised wording.  \n"},
    })


async def test_existing_root_contract_revision_and_native_files_are_preserved(project):
    require_git()
    async with service(project) as case:
        original = await publish_root(case)
        native_root = Path(original["files"]["root"])
        files_before = {item.name: item.read_bytes() for item in native_root.iterdir() if item.is_file()}
        status = await setup_status(case)
        assert status["can_configure"] is True
        assert status["task"] == {
            "id": original["id"], "title": original["contract"]["title"],
            "objective": original["contract"]["objective"], "revision": 2,
        }
        result = await start(case, request(
            goal=original["contract"]["objective"], task_id=original["id"], task_revision=2,
        ))
        await asyncio.wait_for(case.probe.started.wait(), timeout=10)
        assert result["task_id"] == original["id"]
        assert await case.app.state.hub.task(original["id"]) == original
        assert {item.name: item.read_bytes() for item in native_root.iterdir() if item.is_file()} == files_before
        assert len(await case.app.state.hub.list_tasks()) == 1
        historical = await case.app.state.hub.task(original["id"], revision=1)
        assert historical["contract"]["objective"] == "Original exact software objective.\r\n"


@pytest.mark.parametrize("change", [
    {"goal": "Attempt to replace the published objective."},
    {"task_revision": 1}, {"task_id": "another-task"},
    {"task_id": None, "task_revision": None},
])
async def test_existing_root_rejects_changed_goal_or_stale_identity_before_configuration(project, change):
    async with service(project) as case:
        original = await publish_root(case)
        saved = (project / "project.toml").read_bytes()
        body = request(goal=original["contract"]["objective"], task_id=original["id"], task_revision=2)
        response = await case.client.post("/v1/setup/start", json={**body, **change})
        assert response.status_code == 409, response.text
        assert (project / "project.toml").read_bytes() == saved
        assert await case.app.state.hub.task(original["id"]) == original
        assert await case.app.state.hub.list_runs() == []
        assert case.probe.profiles == []
        assert (await setup_status(case))["can_configure"] is True


@pytest.mark.parametrize("history", ["worker", "running", "paused", "stopped"])
async def test_existing_worker_or_run_locks_setup_without_changing_or_resuming_it(project, history):
    async with service(project) as case:
        original = await publish_root(case)
        run = None
        if history == "worker":
            await command(case, "worker.register")
        else:
            run = await command(case, "run.start")
            if history in {"paused", "stopped"}:
                await command(case, "run.pause" if history == "paused" else "run.stop", {"run_id": run["id"]})
        saved = (project / "project.toml").read_bytes()
        workers = await case.app.state.hub.list_workers()
        runs = await case.app.state.hub.list_runs()
        status = await setup_status(case)
        assert status["can_configure"] is False
        assert status["reason"]
        if history != "running":
            assert status["can_drive"] is False
        response = await case.client.post("/v1/setup/start", json=request(
            goal=original["contract"]["objective"], task_id=original["id"], task_revision=2,
        ))
        assert response.status_code == 409, response.text
        assert (project / "project.toml").read_bytes() == saved
        assert await case.app.state.hub.task(original["id"]) == original
        assert await case.app.state.hub.list_workers() == workers
        assert await case.app.state.hub.list_runs() == runs
        assert case.probe.profiles == []
        assert case.app.state.driver.armed == set()
        if run is not None:
            assert (await case.app.state.hub.run(run["id"]))["state"] == history


@pytest.mark.parametrize("change", [
    {"goal": ""}, {"goal": " \r\n\t "}, {"goal": "x" * 20001},
    {"goal": ["not text"]}, {"model": None}, {"model": ""},
    {"effort": None}, {"key": ""}, {"key": True}, {"key": "k" * 201},
    {"task_revision": True}, {"task_revision": "1"}, {"task_revision": 0},
    {"actor": {"role": "runner"}}, {"role": "operator"},
    {"acceptance_policy": "general.manual@1"}, {"limits": {"max_turns": 1}},
], ids=[
    "empty-goal", "blank-goal", "oversize-goal", "nontext-goal", "null-model", "empty-model",
    "null-effort", "empty-key", "boolean-key", "oversize-key", "boolean-revision",
    "string-revision", "zero-revision", "actor-injection", "role-injection",
    "manual-policy-injection", "retired-control-injection",
])
async def test_invalid_setup_controls_are_rejected_before_any_work(project, change):
    saved = (project / "project.toml").read_bytes()
    async with service(project) as case:
        response = await case.client.post("/v1/setup/start", json=request(**change))
        assert response.status_code == 422, response.text
        await pristine(case, saved)


@pytest.mark.parametrize("missing", ["key", "goal", "model", "effort"])
async def test_start_requires_explicit_goal_and_selected_profile(project, missing):
    saved = (project / "project.toml").read_bytes()
    body = request()
    del body[missing]
    async with service(project) as case:
        response = await case.client.post("/v1/setup/start", json=body)
        assert response.status_code == 422, response.text
        await pristine(case, saved)


@pytest.mark.parametrize("body", ["{", "[]", "null", '{"goal":NaN}'])
async def test_malformed_setup_body_is_rejected_without_mutation(project, body):
    saved = (project / "project.toml").read_bytes()
    async with service(project) as case:
        response = await case.client.post("/v1/setup/start", content=body,
                                          headers={"Content-Type": "application/json"})
        assert response.status_code == 422, response.text
        await pristine(case, saved)


async def test_setup_body_limit_prevents_large_goal_from_reaching_catalog(project):
    saved = (project / "project.toml").read_bytes()
    async with service(project, max_command_bytes=512) as case:
        response = await case.client.post("/v1/setup/start", json=request(goal="x" * 513))
        assert response.status_code == 413, response.text
        assert case.catalog.calls == []
        await pristine(case, saved)


@pytest.mark.parametrize("problem", ["authentication", "missing-model", "unsupported-effort"])
async def test_unavailable_model_controls_fail_before_configuration(project, problem):
    saved = (project / "project.toml").read_bytes()
    catalog = Catalog()
    body = request()
    if problem == "authentication":
        catalog.value["authentication_required"] = True
    elif problem == "missing-model":
        body["model"] = "not-in-this-catalog"
    else:
        body["effort"] = "high"  # Supported by another model, but not the selected one.
    async with service(project, catalog=catalog) as case:
        response = await case.client.post("/v1/setup/start", json=body)
        assert 400 <= response.status_code < 500, response.text
        await pristine(case, saved)


@pytest.mark.parametrize("operation", ["catalog", "start"])
async def test_catalog_failure_preserves_project_and_configuration(project, operation):
    saved = (project / "project.toml").read_bytes()
    catalog = Catalog()
    catalog.error = HubError("catalog_unavailable", "Synthetic discovery failure", 503)
    async with service(project, catalog=catalog) as case:
        events = await case.app.state.hub.events()
        response = (await case.client.get("/v1/setup/models") if operation == "catalog"
                    else await case.client.post("/v1/setup/start", json=request()))
        assert response.status_code == 503, response.text
        await pristine(case, saved)
        assert await case.app.state.hub.events() == events


@pytest.mark.parametrize("run_state", ["running", "paused", "stopped"])
async def test_restart_resumes_the_setup_run_and_receipt_retry_never_rearms(project, run_state):
    require_git()
    body = request()
    probe = RunnerProbe()
    async with service(project, probe=probe) as case:
        first = await start(case, body)
        await asyncio.wait_for(probe.started.wait(), timeout=10)
        if run_state != "running":
            await command(case, "run.pause" if run_state == "paused" else "run.stop", {
                "run_id": first["run_id"],
            })
    assert probe.runs == [first["run_id"]]
    saved = (project / "project.toml").read_bytes()
    async with service(project) as restarted:
        resumed = run_state != "stopped"
        driving = run_state == "running"
        assert restarted.app.state.driver.armed == ({first["run_id"]} if resumed else set())
        if driving:
            await asyncio.wait_for(restarted.probe.started.wait(), timeout=10)
        status = await setup_status(restarted)
        assert status["run"] == {
            "id": first["run_id"], "state": run_state, "armed": resumed, "driving": driving,
        }
        assert status["can_configure"] is False and status["can_drive"] is False
        assert restarted.catalog.calls == []
        result = await start(restarted, body)
        assert result["replayed"] is True
        assert result["task_id"] == first["task_id"] and result["run_id"] == first["run_id"]
        assert result["armed"] is resumed and result["driving"] is driving
        assert restarted.catalog.calls == []
        assert restarted.probe.runs == ([first["run_id"]] if driving else [])
        assert (project / "project.toml").read_bytes() == saved
        assert (await restarted.app.state.hub.run(first["run_id"]))["state"] == run_state
        if run_state == "paused":
            await command(restarted, "run.resume", {"run_id": first["run_id"]})
            await asyncio.wait_for(restarted.probe.started.wait(), timeout=10)
            assert restarted.probe.runs == [first["run_id"]]
        elif run_state == "stopped":
            explicit = await restarted.client.post(f"/v1/runs/{first['run_id']}/drive")
            assert explicit.status_code == 409, explicit.text
            assert restarted.probe.runs == []


async def test_setup_recovery_drive_is_hidden_after_any_worker_was_registered(project):
    require_git()
    async with service(project) as case:
        result = await start(case, request())
        await asyncio.wait_for(case.probe.started.wait(), timeout=10)
        await command(case, "worker.register")
    async with service(project, resume_runs=False) as restarted:
        status = await setup_status(restarted)
        assert status["run"]["id"] == result["run_id"]
        assert status["run"]["state"] == "running"
        assert status["can_configure"] is False and status["can_drive"] is False
        assert restarted.probe.runs == []


@pytest.mark.parametrize("failure_at", ["run.start", "receipt-event"])
async def test_task_zero_run_and_receipt_roll_back_together_and_allow_explicit_retry(
        project, monkeypatch, failure_at):
    require_git()
    body = request()
    async with service(project) as case:
        hub = case.app.state.hub
        original_dispatch = hub._dispatch
        original_event = hub._event

        def dispatch(connection, name, payload, actor):
            if name == "run.start":
                raise HubError("synthetic_failure", "Synthetic run creation failure")
            return original_dispatch(connection, name, payload, actor)

        def event(connection, kind, *args, **kwargs):
            if kind == "project.setup":
                raise HubError("synthetic_failure", "Synthetic failure after receipt insertion")
            return original_event(connection, kind, *args, **kwargs)

        with monkeypatch.context() as patch:
            patch.setattr(hub, "_dispatch" if failure_at == "run.start" else "_event",
                          dispatch if failure_at == "run.start" else event)
            response = await case.client.post("/v1/setup/start", json=body)
        assert response.status_code == 409, response.text
        assert await hub.list_tasks() == []
        assert await hub.list_runs() == []
        assert await hub.list_workers() == []
        assert case.probe.runs == []
        assert case.app.state.driver.armed == set()
        status = await setup_status(case)
        assert status["can_configure"] is True
        assert status["task"] is None and status["run"] is None and status["receipt"] is None
        root_id = await hub.db.call(lambda c: c.execute(
            "SELECT root_task_id FROM project_lifecycle WHERE id=1").fetchone()[0])
        assert root_id is None
        assert not any(item["kind"] in {"task.published", "run.started", "project.setup"}
                       for item in await hub.events())
        # Local preparation may remain, but it admits no execution before the commit.
        assert load_config(project).codex.model == body["model"]
        assert status["repository"] == {"enabled": True, "initialized": True}
        result = await start(case, body)
        await asyncio.wait_for(case.probe.started.wait(), timeout=10)
        assert result["replayed"] is False
        assert case.probe.runs == [result["run_id"]]
        assert len(await hub.list_tasks()) == 1
        assert len(await hub.list_runs()) == 1


async def test_other_api_mutations_wait_while_setup_validates_and_prepares(project):
    require_git()
    entered = asyncio.Event()
    release = asyncio.Event()
    mutation_waiting = asyncio.Event()

    class ObservedLock:
        def __init__(self):
            self.lock = asyncio.Lock()

        async def __aenter__(self):
            if self.lock.locked():
                mutation_waiting.set()
            await self.lock.acquire()

        async def __aexit__(self, *_):
            self.lock.release()

    async def catalog(_config, _root):
        entered.set()
        await release.wait()
        return deepcopy(CATALOG)

    saved = (project / "project.toml").read_bytes()
    async with service(project, catalog=catalog) as case:
        case.app.state.setup_lock = ObservedLock()
        pending_start = asyncio.create_task(case.client.post("/v1/setup/start", json=request()))
        pending_command = None
        waiting = None
        try:
            await asyncio.wait_for(entered.wait(), timeout=10)
            pending_command = asyncio.create_task(case.client.post("/v1/commands", json={
                "name": "worker.register", "key": uuid4().hex, "payload": {},
            }))
            waiting = asyncio.create_task(mutation_waiting.wait())
            completed, _ = await asyncio.wait(
                {pending_command, waiting}, timeout=10, return_when=asyncio.FIRST_COMPLETED,
            )
            assert waiting in completed, "The command crossed setup's mutation boundary"
            assert not pending_command.done()
            # Status remains available while preparation waits on external metadata.
            await pristine(case, saved)
        finally:
            release.set()
            pending = [item for item in (pending_start, pending_command, waiting) if item is not None]
            if waiting is not None and not waiting.done():
                waiting.cancel()
            outcomes = await asyncio.gather(*pending, return_exceptions=True)
        start_response, command_response = outcomes[:2]
        assert start_response.status_code == 202, start_response.text
        assert command_response.status_code == 200, command_response.text
        await asyncio.wait_for(case.probe.started.wait(), timeout=10)
        assert len(await case.app.state.hub.list_tasks()) == 1
        assert len(await case.app.state.hub.list_runs()) == 1
        assert len(await case.app.state.hub.list_workers()) == 1
        assert case.probe.runs == [start_response.json()["run_id"]]
