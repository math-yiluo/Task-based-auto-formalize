"""Publish reviewed local source and public TaskHub state through optional Git sync.

This service never runs a model, compiler, or reviewer.
"""
from __future__ import annotations

import asyncio

from . import sync_state
from .errors import HubError
from .git_transport import STATE_DIRECTORY, GitSyncTransport, RemoteHeadMoved
from .json_io import read_json
from .models import GitSyncConfig
from .storage import canonical


class GitSyncService:
    def __init__(self, hub, *, transport=None):
        self.hub = hub
        self.config = getattr(getattr(hub, "config", None), "sync", None) or GitSyncConfig()
        self.transport = transport
        self._lock = asyncio.Lock()
        self._wake = asyncio.Event()
        self._closed = False
        self._last_observation: int | None = None

    def wake(self) -> None:
        self._wake.set()

    async def status(self) -> dict:
        metadata = await sync_state.state(self.hub)
        return {"enabled": self.config.enabled, "remote": self.config.remote,
                "branch": self.hub.config.repository.branch, "state_directory": STATE_DIRECTORY,
                "poll_seconds": self.config.poll_seconds, "syncing": self._lock.locked(),
                **{key: metadata[key] for key in ("project_id", "main_head",
                     "last_export_sequence", "last_success_at", "last_error")}}

    async def _ready(self):
        if not self.config.remote or not self.hub.config.repository.enabled:
            raise HubError("SYNC_CONFIGURATION_REQUIRED", "Configure a remote and managed repository first")
        metadata = await sync_state.state(self.hub)
        if metadata["remote"] is not None and metadata["remote"] != self.config.remote:
            raise HubError("SYNC_REMOTE_CHANGED", "This ledger already belongs to another synchronization remote")
        if metadata["remote"] is None:
            await sync_state.update_state(self.hub, remote=self.config.remote)
        if self.transport is None:
            self.transport = GitSyncTransport(
                self.hub.state_dir, self.config.remote, executable=self.hub.config.repository.executable,
                credential_helper=self.config.credential_helper, timeout_seconds=self.config.timeout_seconds)
        await asyncio.to_thread(self.transport.initialize)

    async def _publish_project(self, source_commit: str, contents: dict[str, bytes], sequence: int) -> dict:
        metadata = await sync_state.state(self.hub)
        branch = self.hub.config.repository.branch
        head = await self.transport.fetch(branch)
        digest = sync_state.file_digest(contents)
        if head != metadata["main_head"]:
            if head is not None and head == metadata["pending_head"]:
                # Only the exact commit persisted before our push can recover a
                # lost acknowledgement. An independent same-tree commit cannot.
                observed = await asyncio.to_thread(self.transport.files, head)
                prefix = STATE_DIRECTORY + "/"
                observed_state = {path[len(prefix):]: body for path, body in observed.items()
                                  if path.startswith(prefix)}
                marker = read_json(observed_state.get("sync.json", b""))
                if (marker.get("project_id") != metadata["project_id"]
                        or marker.get("state_directory") != STATE_DIRECTORY
                        or sync_state.file_digest(observed_state) != metadata["pending_digest"]):
                    raise RemoteHeadMoved("The pending publication does not match its recorded TaskHub export")
                metadata = await sync_state.update_state(self.hub, main_head=head,
                    last_export_digest=metadata["pending_digest"], pending_digest=None, pending_head=None,
                    last_export_sequence=int(marker["ledger_sequence"]))
            elif (metadata["main_head"] is None and head is not None and await self.hub.db.call(
                    lambda c: self.hub._repository_head_recorded(c, head))):
                pass  # Adopt only a recorded review baseline on first synchronization.
            else:
                raise RemoteHeadMoved("The remote public branch changed independently or was removed; it will not be overwritten")
        if (head is not None and metadata["main_head"] == head
                and metadata["last_export_digest"] == digest):
            await sync_state.update_state(self.hub, pending_digest=None, pending_head=None)
            return {"head": head, "changed": False}

        async def prepared(commit: str) -> None:
            await sync_state.update_state(self.hub, pending_head=commit, pending_digest=digest)

        result = await self.transport.publish_project(
            self.hub._repository(), source_commit, branch, contents, expected_head=head,
            message=f"Publish reviewed source and TaskHub state through event {sequence}\n",
            on_prepared=prepared)
        await sync_state.update_state(self.hub, main_head=result["head"],
                                     last_export_digest=digest, last_export_sequence=sequence,
                                     pending_digest=None, pending_head=None)
        return result

    async def once(self) -> dict:
        if not self.config.enabled:
            return {"enabled": False, "state": "disabled"}
        async with self._lock:
            try:
                await self._ready()
                repository_status = await self.hub.repository_status()
                if not repository_status.get("initialized") or repository_status.get("reconciliation_required"):
                    raise HubError("REPOSITORY_NOT_READY", "Initialize or reconcile the local publication repository before sync")
                commit = repository_status["head"]
                contents, sequence = await sync_state.export_files(self.hub, main_commit=commit)
                public = await self._publish_project(commit, contents, sequence)
                await sync_state.update_state(self.hub, last_success_at=self.hub.clock(), last_error=None)
                return {"enabled": True, "state": "synced",
                        "main": public, "source_commit": commit,
                        "state_directory": STATE_DIRECTORY, "ledger_sequence": sequence}
            except asyncio.CancelledError:
                raise
            except (HubError, ValueError, OSError) as error:
                detail = {"code": getattr(error, "code", type(error).__name__), "message": str(error)[:2000]}
                await sync_state.update_state(self.hub, last_error=canonical(detail))
                return {"enabled": True, "state": "error", "error": detail}

    async def run(self) -> None:
        """Run only the configured sync service; never arm or resume a model run."""
        if not self.config.enabled:
            return
        next_poll = 0.0
        loop = asyncio.get_running_loop()
        while not self._closed:
            observation = await self.hub.db.call(lambda c:
                c.execute("SELECT COALESCE(MAX(sequence),0) FROM events").fetchone()[0])
            changed = observation != self._last_observation
            if changed or self._wake.is_set() or loop.time() >= next_poll:
                self._wake.clear()
                await self.once()
                self._last_observation = observation
                next_poll = loop.time() + self.config.poll_seconds
            # Poll local event counters cheaply. Network calls are batched and
            # only triggered by changes or the configured remote poll interval.
            try:
                await asyncio.wait_for(self._wake.wait(), timeout=2)
            except TimeoutError:
                pass

    def stop(self) -> None:
        self._closed = True
        self._wake.set()
