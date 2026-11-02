"""The coordination thread never waits for the content lock that archive moves and Library views hold."""
import shutil
import threading
import time
from contextlib import contextmanager
from uuid import uuid4

import pytest

from formal_geyser.coordination.content import NativeContentStore
from tests.coordination.publication_helpers import approve
from tests.coordination.test_archive_publication import archive_case as archive_case  # noqa: F401
from tests.coordination.test_archive_publication import quiet
from tests.coordination.test_staging_preparation import submitted as submitted  # noqa: F401

needs_git = pytest.mark.skipif(shutil.which("git") is None, reason="Native Git is required")


def observe_lock(monkeypatch):
    entered = []
    original = NativeContentStore._locked

    @contextmanager
    def observed(self):
        entered.append(threading.current_thread().name)
        with original(self):
            yield
    monkeypatch.setattr(NativeContentStore, "_locked", observed)
    return entered


def test_task_reads_and_note_folders_do_not_wait_for_a_held_content_lock(tmp_path):
    content = NativeContentStore(tmp_path / ".task-based")
    task_id = uuid4().hex
    view = content.task_files(task_id, 1, {"title": "Locked", "objective": "Read without the lock.", "context": ""})
    held, release = threading.Event(), threading.Event()

    def holder():
        with content._locked():
            held.set()
            release.wait(30)
    thread = threading.Thread(target=holder)
    thread.start()
    assert held.wait(10)
    try:
        started = time.monotonic()
        assert content.read_task(task_id, 1, view.contract_sha256)["objective"] == "Read without the lock."
        content.worker_notes(uuid4().hex)
        assert time.monotonic() - started < 5
    finally:
        release.set()
        thread.join()


@needs_git
async def test_staging_on_the_coordination_thread_takes_no_content_lock(submitted, monkeypatch):
    entered = observe_lock(monkeypatch)
    await quiet(submitted.hub, submitted.author)
    assert not any(name.startswith("taskhub-db") for name in entered)


@needs_git
async def test_the_library_pointer_moves_off_the_coordination_thread(archive_case, monkeypatch):
    case = archive_case
    threads = []
    original = NativeContentStore.publish_current_view

    def observed(self, *args, **kwargs):
        threads.append(threading.current_thread().name)
        return original(self, *args, **kwargs)
    monkeypatch.setattr(NativeContentStore, "publish_current_view", observed)
    await approve(case.hub, case.reviewer)
    view = await case.hub.public_content()
    assert view.commit == case.hub._repository().head()
    assert threads and not any(name.startswith("taskhub-db") for name in threads)
