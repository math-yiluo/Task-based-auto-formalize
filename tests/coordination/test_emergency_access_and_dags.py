"""Approved project reads and stale graph merges retain write and evidence guards."""

import pytest

from formal_geyser.coordination.errors import HubError
from formal_geyser.coordination.models import Actor, Role

from .test_gateway_runtime import call
from .test_gateway_runtime import project as project


async def test_project_read_and_copy_do_not_grant_credential_or_foreign_writes(project, tmp_path):
    hub, task, attempt, runner = project
    note = hub.state_dir / "workers/other/work/.private/note.md"
    note.parent.mkdir(parents=True)
    note.write_text("Readable project research.", encoding="utf8")
    assert (await call(runner, attempt, "workspace_read", {"path": str(note)}, "read"))["text"] == "Readable project research."
    # The same research is readable through the project resource root.
    result = await call(runner, attempt, "resource_read", {
        "root": "project", "path": note.relative_to(hub.root).as_posix()}, "resource-read")
    assert result["text"] == "Readable project research."
    with pytest.raises(ValueError):
        await call(runner, attempt, "workspace_write", {"path": str(note), "text": "not owned"}, "write")
    stale = runner._worker_actor(attempt).model_copy(update={"fencing_token": attempt["fencing_token"] + 1})
    from formal_geyser.coordination.runtime.native_files import native_file
    with pytest.raises(HubError) as failure:
        await native_file(hub, stale, runner.resources, str(note))
    assert failure.value.code == "STALE_BINDING"
    assert Actor(role=Role.RUNNER, worker_id=attempt["worker_id"]).worker_id


async def test_missing_resource_has_structured_not_found(project):
    hub, _, attempt, runner = project
    with pytest.raises(HubError) as failure:
        await call(runner, attempt, "resource_read", {"root": "project", "path": "missing.md"}, "missing")
    assert failure.value.code == "NOT_FOUND"
