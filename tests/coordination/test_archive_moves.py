"""Publication moves archives after the coordination transaction commits, off its thread, in
the Hub's background task: no command waits for them."""
import asyncio
import json
import threading
from pathlib import Path

from formal_geyser.coordination.artifact_files import ArtifactFiles
from tests.coordination.publication_helpers import approve
from tests.coordination.test_archive_publication import archive_case as archive_case  # noqa: F401
from tests.coordination.test_archive_publication import command


async def pending_moves(hub):
    return await hub.db.read(lambda c: [row[0] for row in c.execute("SELECT record_id FROM archive_relocations")])


async def failures(hub, artifact_id):
    return await hub.db.read(lambda c: c.execute(
        "SELECT count(*) FROM events WHERE kind='archive.relocation_failed' AND object_id=?", (artifact_id,)).fetchone()[0])


async def as_command(hub, function):
    """Apply inside one transaction the way a command does: archive moves are recorded, not made."""
    def apply(c):
        hub._defer_archive_moves.active = True
        try:
            return function(c)
        finally:
            hub._defer_archive_moves.active = False
    return await hub.db.call(apply, write=True)


def observe_move_threads(monkeypatch):
    threads = []
    original = ArtifactFiles.relocate

    def observed(self, *args, **kwargs):
        threads.append(threading.current_thread().name)
        return original(self, *args, **kwargs)
    monkeypatch.setattr(ArtifactFiles, "relocate", observed)
    return threads


async def test_published_archives_move_after_commit_off_the_coordination_thread(archive_case, monkeypatch):
    case = archive_case
    threads = observe_move_threads(monkeypatch)
    await approve(case.hub, case.reviewer)
    await case.hub.archive_moves_settled()
    assert threads and not any(name.startswith("taskhub-db") for name in threads)
    artifact = await case.hub.artifact(case.candidate["artifact_id"])
    assert Path(artifact["content_file"]["path"]) == case.root / "artifacts" / artifact["id"] / "1/record.json"
    assert await pending_moves(case.hub) == []


async def test_a_failed_move_stays_recorded_while_reads_continue_and_a_retry_finishes_it(archive_case, monkeypatch):
    case = archive_case
    original = ArtifactFiles.relocate

    def unavailable(self, *args, **kwargs):
        raise OSError("Synthetic storage interruption")
    monkeypatch.setattr(ArtifactFiles, "relocate", unavailable)
    await approve(case.hub, case.reviewer)
    await case.hub.archive_moves_settled()
    artifact_id = case.candidate["artifact_id"]
    assert await pending_moves(case.hub) == [artifact_id]
    staged = await case.hub.artifact(artifact_id)
    assert Path(staged["content_file"]["path"]).is_relative_to(case.root / "staging")
    assert staged["status"] == "accepted"
    assert await failures(case.hub, artifact_id) == 1
    monkeypatch.setattr(ArtifactFiles, "relocate", original)
    assert await case.hub.reconcile_archive_relocations() == [artifact_id]
    moved = await case.hub.artifact(artifact_id)
    assert Path(moved["content_file"]["path"]) == case.root / "artifacts" / artifact_id / "1/record.json"
    assert await pending_moves(case.hub) == []


async def test_reads_between_the_file_move_and_its_ledger_update_follow_the_journal(archive_case, monkeypatch):
    case = archive_case
    hub = case.hub
    original = type(hub)._finish_relocation

    def interrupted(self, c, artifact_id, plan, target):
        raise OSError("Synthetic stop after the files moved")
    monkeypatch.setattr(type(hub), "_finish_relocation", interrupted)
    await approve(hub, case.reviewer)  # The publication itself is committed and reported.
    await hub.archive_moves_settled()
    artifact_id = case.candidate["artifact_id"]
    assert await pending_moves(hub) == [artifact_id]
    # The files already moved; the ledger still names the staging path and pinned manifest.
    read = await hub.artifact(artifact_id)
    assert read["status"] == "accepted" and read["data"]["bundle"]["lean_path"] == "Fixture.lean"
    monkeypatch.setattr(type(hub), "_finish_relocation", original)
    assert await hub.reconcile_archive_relocations() == [artifact_id]
    moved = await hub.artifact(artifact_id)
    assert Path(moved["content_file"]["path"]) == case.root / "artifacts" / artifact_id / "1/record.json"
    assert await pending_moves(hub) == []


async def test_a_failed_move_is_retried_by_a_later_command_once_its_delay_passes(archive_case, monkeypatch):
    case = archive_case
    original = ArtifactFiles.relocate

    def unavailable(self, *args, **kwargs):
        raise OSError("Synthetic storage interruption")
    monkeypatch.setattr(ArtifactFiles, "relocate", unavailable)
    await approve(case.hub, case.reviewer)
    await case.hub.archive_moves_settled()
    artifact_id = case.candidate["artifact_id"]
    monkeypatch.setattr(ArtifactFiles, "relocate", original)
    task = {"contract": {"title": "Later command", "objective": "Observe the retry."}}
    await command(case.hub, "task.publish", task)
    await case.hub.archive_moves_settled()
    assert await pending_moves(case.hub) == [artifact_id]  # Not before the retry delay.
    case.hub._archive_relocation_retry_at = 0.0
    await command(case.hub, "task.publish", task)
    await case.hub.archive_moves_settled()
    assert await pending_moves(case.hub) == []
    moved = await case.hub.artifact(artifact_id)
    assert Path(moved["content_file"]["path"]) == case.root / "artifacts" / artifact_id / "1/record.json"
    assert await failures(case.hub, artifact_id) == 1


async def test_a_withdrawal_while_the_refusal_is_still_moving_keeps_the_first_outcome(archive_case, monkeypatch):
    case = archive_case
    hub, artifact_id = case.hub, case.candidate["artifact_id"]
    threads = observe_move_threads(monkeypatch)
    await as_command(hub, lambda c: hub._archive_submission(c, artifact_id, "rejected", "Synthetic refusal."))
    await as_command(hub, lambda c: hub._archive_submission(c, artifact_id, "withdrawn", "Synthetic withdrawal."))
    assert threads == [] and await pending_moves(hub) == [artifact_id]  # Nothing moved inside either transaction.
    assert await hub.reconcile_archive_relocations() == [artifact_id]
    archived = Path((await hub.artifact(artifact_id))["content_file"]["path"])
    history = case.root / "submission-history" / artifact_id
    assert archived == history / "1/record.json"
    assert json.loads((archived.parent / "history.json").read_bytes())["disposition"] == "rejected"
    events = [json.loads(path.read_bytes())["disposition"] for path in (history / "events").glob("*.json")]
    assert sorted(events) == ["rejected", "withdrawn"]
    assert await pending_moves(hub) == [] and await failures(hub, artifact_id) == 0


async def test_a_record_read_that_found_the_old_folder_just_before_the_move_reads_again(archive_case, monkeypatch):
    case = archive_case
    hub = case.hub

    def interrupted(self, c, artifact_id, plan, target):
        raise OSError("Synthetic stop after the files moved")
    monkeypatch.setattr(type(hub), "_finish_relocation", interrupted)
    await approve(hub, case.reviewer)
    artifact_id = case.candidate["artifact_id"]
    record = await hub.db.call(lambda c: hub._get(c, "artifacts", artifact_id))
    resolve = ArtifactFiles.resolve
    calls = []

    def old_folder_once(self, path, *args):
        calls.append(path)
        return path if len(calls) == 1 else resolve(self, path, *args)
    monkeypatch.setattr(ArtifactFiles, "resolve", old_folder_once)
    body = await hub.db.call(lambda c: hub._record_body(c, record))
    assert body["bundle"]["lean_path"] == "Fixture.lean" and len(calls) == 2


async def test_commands_return_while_archives_move_and_a_move_recorded_meanwhile_gets_its_pass(
        archive_case, monkeypatch):
    # In the Linux pilot a move recorded during a long pass made every later command wait for the
    # whole queue: nothing else was done for ten minutes.
    case = archive_case
    hub, artifact_id = case.hub, case.candidate["artifact_id"]
    entered, release = threading.Event(), threading.Event()
    original, passes = ArtifactFiles.relocate, []

    def held(self, *args, **kwargs):
        entered.set()
        assert release.wait(20)
        return original(self, *args, **kwargs)
    monkeypatch.setattr(ArtifactFiles, "relocate", held)
    reconcile = hub.reconcile_archive_relocations

    async def counted():
        passes.append(True)
        return await reconcile()
    monkeypatch.setattr(hub, "reconcile_archive_relocations", counted)
    settled = hub.archive_moves_settled

    async def returned():
        return None  # approve() returns as its commands do, while the archive is still moving.
    monkeypatch.setattr(hub, "archive_moves_settled", returned)
    try:
        await approve(hub, case.reviewer)
        assert await asyncio.to_thread(entered.wait, 20)
        task = {"contract": {"title": "Later command", "objective": "Runs while the archive moves."}}
        async with asyncio.timeout(10):
            await command(hub, "task.publish", task)
        assert await pending_moves(hub) == [artifact_id]
        hub._archive_relocations_pending = True  # As a command recording a move during the pass does.
    finally:
        release.set()
    async with asyncio.timeout(20):
        await settled()
    assert len(passes) == 2 and await pending_moves(hub) == []
    moved = await hub.artifact(artifact_id)
    assert Path(moved["content_file"]["path"]) == case.root / "artifacts" / artifact_id / "1/record.json"


async def test_a_move_recorded_by_a_transaction_still_open_is_not_skipped(archive_case):
    # A pass reads the recorded moves between transactions on the coordination thread, so one
    # recorded by a transaction that commits after the pass started is either read or flagged again.
    case = archive_case
    hub, artifact_id = case.hub, case.candidate["artifact_id"]
    recorded, proceed = threading.Event(), threading.Event()

    def refuse(c):
        hub._archive_submission(c, artifact_id, "rejected", "Synthetic refusal.")
        recorded.set()
        assert proceed.wait(20)  # The transaction stays open while a pass starts.

    refusing = asyncio.create_task(as_command(hub, refuse))
    assert await asyncio.to_thread(recorded.wait, 20)
    passing = asyncio.create_task(hub.reconcile_archive_relocations())
    await asyncio.sleep(0.2)
    proceed.set()
    await refusing
    assert await passing == [artifact_id] or hub._archive_moves_due()
    await hub.finish_deferred_files("refusal")
    await hub.archive_moves_settled()
    assert await pending_moves(hub) == []
