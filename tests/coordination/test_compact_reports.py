"""Exact compressed reports and small replayable validation receipts."""
import json
from uuid import uuid4

from formal_geyser.coordination.models import Actor, Command, Role
from tests.coordination.test_archive_publication import archive_case as archive_case


async def test_validation_receipt_contains_only_latest_evidence_and_replays_exactly(archive_case):
    hub, aid = archive_case.hub, archive_case.candidate["artifact_id"]
    details = {"compilation": {"stdout": "exact compiler output\n" * 50000}, "proof_accepted": False}
    command = Command(name="artifact.accept", key=uuid4().hex, payload={
        "artifact_id": aid, "policy": "general.peer@1", "verdict": "accepted", "details": details})
    actor = Actor(role=Role.VALIDATOR)
    first = await hub.execute(command, actor)
    assert len(first["evidence"]) == 1 and "data" not in first
    assert len(json.dumps(first)) < 8192
    assert await hub.execute(command, actor) == first
    assert len((await hub.artifact(aid))["evidence"]) == 2
    saved = await hub.db.read(lambda c: c.execute("SELECT response_json FROM receipts WHERE key=?", (command.key,)).fetchone()[0])
    assert json.loads(saved) == first
    report = first["evidence"][0]["details"]["report_file"]
    assert hub.reports.read(report["sha256"]) == details
