import pytest
from pydantic import ValidationError

from formal_geyser.application import LeanApplication
from formal_geyser.coordination.models import (
    Actor,
    Role,
    TaskContract,
)
from formal_geyser.paths import lean_name, relative_path
from formal_geyser.plugin import LeanPlugin
from formal_geyser.registry import (
    ObjectRegistry,
    RegistryError,
    has_approval,
    read_snapshot,
)
from formal_geyser.schemas import (
    EnvironmentRecord,
    LeanTaskPayload,
)
from tests.conftest import FakeHub


@pytest.mark.parametrize("path", ["../x.lean", "C:/x.lean", "/x.lean", "A\\B.lean", "a//b", "./x",
                                  "NUL.txt", "a/COM1.lean", "a/x.", "a/x ", "a:stream", "A/../B"])
def test_portable_paths_reject_ambiguous_inputs(path):
    with pytest.raises(ValueError):
        relative_path(path)


@pytest.mark.parametrize("name", ["A; import X", "A\nB", "A..B", "_root_.A;", "A/B", "", "A.1"])
def test_names_reject_source_and_argument_injection(name):
    with pytest.raises(ValueError):
        lean_name(name)


def test_names_and_paths():
    assert lean_name("Workspace.Target'") == "Workspace.Target'"
    assert relative_path("Workspace/Target.lean") == "Workspace/Target.lean"


def test_no_reasoning_phase_schema():
    with pytest.raises(ValidationError):
        LeanTaskPayload.model_validate({"direction": "forward"})


def test_sorry_cannot_be_allowed(proof_case):
    hub, _, _ = proof_case
    body = hub.artifacts["environment-1"]["data"] | {"allowed_axioms": ["sorryAx"]}
    with pytest.raises(ValidationError):
        EnvironmentRecord.model_validate(body)


def test_snapshot_case_collisions_rejected(tmp_path):
    hub = FakeHub(tmp_path)
    sha = hub.blobs.put(b"source metadata")
    manifest = hub.blobs.put_json({"files": [{"path": "A.lean", "sha256": sha},
                                               {"path": "a.lean", "sha256": sha}]})
    with pytest.raises(RegistryError, match="colliding"):
        read_snapshot(hub.blobs, manifest)


def test_snapshot_limit_checked(tmp_path):
    hub = FakeHub(tmp_path)
    sha = hub.blobs.put(b"metadata")
    manifest = hub.blobs.put_json({"files": [{"path": "A.lean", "sha256": sha}]})
    with pytest.raises(RegistryError, match="byte"):
        read_snapshot(hub.blobs, manifest, max_bytes=2)


async def test_source_import_uses_native_literature_and_preserves_bytes(tmp_path):
    hub = FakeHub(tmp_path)
    source = tmp_path / "source.md"
    body = b"Source documentation fixture.\r\n"
    source.write_bytes(body)
    await ObjectRegistry(hub).import_source(source, Actor(role=Role.OPERATOR))
    command = hub.commands[0][0]
    assert command.name == "literature.register"
    assert command.payload["path"] == str(source.resolve())
    assert command.payload["copy"] is True
    assert source.read_bytes() == body
    assert not hub.artifacts


def test_approval_requires_live_exact_policy():
    artifact = {"accepted": True, "status": "accepted", "evidence": [{"policy": "lean.binding@1", "verdict": "accepted"}]}
    assert has_approval(artifact, "lean.binding@1")
    assert not has_approval(artifact, "lean.specification@1")
    assert not has_approval(artifact | {"status": "withdrawn"}, "lean.binding@1")
    assert not has_approval(artifact | {"evidence": []}, "lean.binding@1")


def test_plugin_requires_frozen_explicit_refs(proof_case):
    _, task, _ = proof_case
    contract = TaskContract.model_validate(task["contract"])
    LeanPlugin().validate_contract(contract)
    with pytest.raises(ValueError, match="explicit"):
        LeanPlugin().validate_contract(contract.model_copy(update={"inputs": []}))


async def test_application_validates_and_delegates_publish(tmp_path):
    hub = FakeHub(tmp_path)
    application = LeanApplication(hub)
    actor = Actor(role=Role.OPERATOR)
    contract = TaskContract(title="Metadata review", objective="Review supplied metadata.", domain="lean",
                            acceptance_policy="lean.peer@1")
    await application.publish(contract, actor, key="publish-1")
    assert [(command.name, command.key) for command, _ in hub.commands] == [("task.publish", "publish-1")]
    assert hub.commands[0][0].payload["contract"] == contract.model_dump(mode="json")

def test_latest_receipt_and_qualifications_control_approval():
    artifact = {"accepted": True, "status": "accepted", "evidence": [
        {"policy": "lean.binding@1", "verdict": "accepted"},
        {"policy": "lean.binding@1", "verdict": "rejected"},
    ]}
    assert not has_approval(artifact, "lean.binding@1")
    artifact["evidence"].append({"policy": "lean.binding@1", "verdict": "accepted", "qualifications": ["incomplete"]})
    assert not has_approval(artifact, "lean.binding@1")

def test_snapshot_capture_limits_are_applied_before_oversized_file_storage(tmp_path):
    from formal_geyser.registry import snapshot_sources
    hub = FakeHub(tmp_path)
    source = tmp_path / "input"
    source.mkdir()
    (source / "document.txt").write_bytes(b"oversized")
    with pytest.raises(RegistryError, match="byte limit"):
        snapshot_sources(hub, source, max_bytes=2)
    assert not [path for path in hub.blobs.root.rglob("*") if path.is_file()]


def test_bounded_capture_matches_core_snapshot_format(tmp_path):
    from formal_geyser.registry import snapshot_sources
    hub = FakeHub(tmp_path)
    source = tmp_path / "input"
    source.mkdir()
    (source / "document.txt").write_bytes(b"metadata")
    (source / ".lake").mkdir()
    (source / ".lake" / "ignored").write_bytes(b"build output")
    assert snapshot_sources(hub, source) == hub.blobs.snapshot(source)
    with pytest.raises(RegistryError, match="file limit"):
        (source / "another.txt").write_bytes(b"metadata")
        snapshot_sources(hub, source, max_files=1)


def test_snapshot_size_rejected_before_materializing_large_blob(tmp_path, monkeypatch):
    hub = FakeHub(tmp_path)
    body = hub.blobs.put(b"oversized metadata")
    manifest = hub.blobs.put_json({"files": [{"path": "source.txt", "sha256": body}]})
    calls = []
    original = hub.blobs.get
    def observe(sha):
        calls.append(sha)
        return original(sha)
    monkeypatch.setattr(hub.blobs, "get", observe)
    with pytest.raises(RegistryError, match="byte limit"):
        read_snapshot(hub.blobs, manifest, max_bytes=1)
    assert body not in calls
