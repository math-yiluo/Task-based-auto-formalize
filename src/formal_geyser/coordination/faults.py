"""Automatically retry closed execution without changing worker intent."""
from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import re
import time
from typing import TYPE_CHECKING, Any, Callable
from uuid import uuid4

from .errors import HubError
from .models import BACKENDS, Actor, Role
from .storage import atomic_json

logger = logging.getLogger(__name__)

# A durable copy of each hold, written before the database row. Each hold has an ID;
# Resume records the IDs it released, so a late write or a leftover file never revives it.
# Codex and Claude bill different accounts, so each backend has its own hold.
HOLD_MARKER = "dispatch-hold.json"
HOLD_MARKERS = {"codex": HOLD_MARKER, "claude": "claude-dispatch-hold.json"}
HOLD_TABLES = {"codex": "dispatch_hold", "claude": "claude_dispatch_hold"}
UNREADABLE_HOLDS = {
    "codex": "A dispatch hold was recorded but its details could not be read: check the Codex account, then Resume.",
    "claude": "A dispatch hold was recorded but its details could not be read: check the Claude subscription, then Resume.",
}

# Explicit account errors stop new dispatch to that backend's workers until the operator resumes.
ACCOUNT_HOLDS = {
    "usageLimitExceeded": "Usage limit reached: switch the Codex account, then Resume.",
    "unauthorized": "Codex sign-in required: sign in, then Resume.",
    "claudeUsageLimit": ("Claude subscription limit reached; usage credits are never used. "
                         "Wait for the limit to reset, then Resume."),
    "claudeSignInRequired": ("Claude sign-in required: run claude with the configured Claude home, "
                             "sign in with /login, then Resume."),
    "claudeSubscriptionRequired": ("Claude is not signed in with a subscription and API billing is disabled: "
                                   "remove API credentials, sign in with /login, then Resume."),
}
HOLD_BACKENDS = {"usageLimitExceeded": "codex", "unauthorized": "codex", "claudeUsageLimit": "claude",
                 "claudeSignInRequired": "claude", "claudeSubscriptionRequired": "claude"}
# How long a reading of the machine's sandboxing (`environment_probe`) is reused.
ENVIRONMENT_SECONDS = 5.0


class FaultIsolation:
    _pending_dispatch_holds: dict[str, dict[str, Any]]  # Account holds not yet written, by backend.
    # The service sets it to native_permissions.sandbox_blocker; without it nothing is checked.
    environment_probe: Callable[[], str | None] | None = None
    _environment: tuple[float, str | None, int | None] | None = None  # (read at, blocker, since)

    if TYPE_CHECKING:
        state_dir: Any
        _get: Any
        _event: Any
        clock: Any
        db: Any
        _lifecycle_command: Any
        _helper: Any
        _control_scope: Any
        _scope_admits: Any
        _dispatch: Any
        prepare_workspace_snapshots: Any
        with_workspace_snapshots: Any
        _with_deferred_files: Any
        finish_deferred_files: Any
        _recompute_task: Any

    @staticmethod
    def _latest_event(c, object_id, *kinds):
        """The newest event of these kinds about an object (kind, created_at, payload_json)."""
        return c.execute(f"SELECT kind,created_at,payload_json FROM events WHERE object_id=? AND kind IN "
                         f"({','.join('?' * len(kinds))}) ORDER BY sequence DESC LIMIT 1",
                         (object_id, *kinds)).fetchone()

    def _latest_payload(self, c, object_id, *kinds) -> dict:
        """The payload of the newest event of these kinds about an object; {} without one."""
        event = self._latest_event(c, object_id, *kinds)
        return json.loads(event["payload_json"]) if event else {}

    def _hold_worker_usage(self, c, worker_id, reason):
        """Retain accounting uncertainty without changing execution or intent."""
        worker = self._get(c, "workers", worker_id)
        c.execute("INSERT INTO worker_usage_holds VALUES(?,?,?,?) ON CONFLICT(worker_id) "
                  "DO UPDATE SET reason=excluded.reason", (worker_id, worker["run_id"], reason, self.clock()))
        c.execute("UPDATE runs SET usage_uncertain=1 WHERE id=?", (worker["run_id"],))
        self._event(c, "worker.accounting_held", worker_id, {"reason": reason}, worker_id=worker_id)

    def _hold_helper_usage(self, c, helper, reason):
        c.execute("INSERT INTO helper_usage_holds VALUES(?,?,?,?) ON CONFLICT(helper_id) "
                  "DO UPDATE SET reason=excluded.reason", (helper["id"], helper["run_id"], reason, self.clock()))
        c.execute("UPDATE runs SET usage_uncertain=1 WHERE id=?", (helper["run_id"],))

    async def note_helper_accounting(self, helper_id, reason):
        def note(c):
            helper = self._helper(c, helper_id)
            self._hold_helper_usage(c, helper, reason)
            self._event(c, "helper.accounting_warning", helper_id, {"reason": reason})
        await self.db.call(note, write=True)

    async def note_worker_accounting(self, worker_id, reason):
        await self.db.call(lambda c: self._hold_worker_usage(c, worker_id, reason), write=True)

    async def note_helper_quiet(self, helper_id):
        def note(c):
            helper = self._helper(c, helper_id)
            # A retained decision waiting to be committed, as for a paused worker, stays pending.
            if helper["state"] == "prepared" or (helper["state"] == "running" and not helper["raw_result_json"]):
                c.execute("UPDATE helper_intents SET state='uncertain',reason='closed_before_completion' WHERE id=?", (helper_id,))
                if helper["state"] == "running":
                    self._hold_helper_usage(c, helper, "Native helper stopped before its result could be retained")
                helper = self._helper(c, helper_id)
            if helper["state"] == "uncertain":
                self._event(c, "helper.execution_closed", helper_id, {"process_quiet": True})
        await self.db.call(note, write=True)

    async def note_owned_process(self, kind, subject_id, receipt):
        if receipt is None:
            return
        if kind not in {"worker", "helper"} or receipt.get("format") != 2:
            raise ValueError("Invalid runtime process receipt")
        await self.db.call(lambda c: self._event(c, "execution.process_owned", subject_id,
                                               {"kind": kind, "receipt": receipt}), write=True)

    async def hold_dispatch(self, code, detail, *, subject_id, worker_id=None, backend=None):
        """Stop sending new model input to one backend until the operator resumes; in-flight turns continue.

        The decision takes effect in this process first, then is written to the marker
        file and the database; the admission loop retries a failed database write.
        Every report gets a new ID, so it is never mistaken for a hold that a concurrent
        Resume already released; a hold already in the database stays the active one.
        """
        backend = backend or HOLD_BACKENDS[code]
        hold = {"hold_id": uuid4().hex, "code": code, "message": ACCOUNT_HOLDS[code],
                "subject_id": subject_id, "worker_id": worker_id,
                "created_at": self.clock(), "detail": str(detail)[:1000]}
        self._pending_dispatch_holds[backend] = hold
        try:
            # Survives a restart even if the database write below fails.
            atomic_json(self.state_dir / HOLD_MARKERS[backend], hold)
        except OSError as error:
            logger.warning("Dispatch hold marker could not be written: %s", type(error).__name__)
        return await self.persist_pending_dispatch_hold(backend)

    @staticmethod
    def _hold_released(c, hold_id):
        return c.execute("SELECT 1 FROM released_dispatch_holds WHERE hold_id=?", (hold_id,)).fetchone() is not None

    def _insert_dispatch_hold(self, c, hold, backend, *, restored=False):
        c.execute(f"INSERT INTO {HOLD_TABLES[backend]}(id,hold_id,code,message,subject_id,worker_id,created_at) "
                  "VALUES(1,?,?,?,?,?,?)",
                  (hold["hold_id"], hold["code"], hold["message"], hold["subject_id"], hold["worker_id"], hold["created_at"]))
        self._event(c, "dispatch.held", hold["subject_id"] or "dispatch-hold",
                    {"hold_id": hold["hold_id"], "code": hold["code"], "backend": backend,
                     "detail": hold.get("detail", ""), **({"restored_from_marker": True} if restored else {})},
                    worker_id=hold["worker_id"])

    async def persist_pending_dispatch_hold(self, backend=None):
        """Write holds that so far exist only in memory; True when this call stored one."""
        stored = False
        for name in ([backend] if backend else list(self._pending_dispatch_holds)):
            pending = self._pending_dispatch_holds.get(name)
            if pending is None:
                continue

            def persist(c, pending=pending, name=name):
                # Checked in the same transaction as the write: a Resume that already
                # released this hold wins, even if this write was queued before it.
                if (self._hold_released(c, pending["hold_id"])
                        or c.execute(f"SELECT 1 FROM {HOLD_TABLES[name]} WHERE id=1").fetchone()):
                    return False
                self._insert_dispatch_hold(c, pending, name)
                return True
            stored = await self.db.call(persist, write=True) or stored
            if self._pending_dispatch_holds.get(name) is pending:
                del self._pending_dispatch_holds[name]
        return stored

    def _read_hold_marker(self, backend="codex"):
        """The marker's hold with only well-typed fields; {} if unreadable, None if absent."""
        try:
            record = json.loads((self.state_dir / HOLD_MARKERS[backend]).read_text(encoding="utf-8"))
        except FileNotFoundError:
            return None
        except (OSError, ValueError):
            return {}
        if not isinstance(record, dict):
            return {}
        clean = {key: record[key] for key in ("hold_id", "code", "subject_id", "worker_id", "detail")
                 if isinstance(record.get(key), str)}
        code = clean.get("code")
        if not isinstance(code, str) or HOLD_BACKENDS.get(code) != backend:
            clean.pop("code", None)
        if type(record.get("created_at")) is int:
            clean["created_at"] = record["created_at"]
        return clean

    def _write_marker_id(self, marker, hold_id, backend="codex"):
        try:
            atomic_json(self.state_dir / HOLD_MARKERS[backend], {**marker, "hold_id": hold_id})
        except OSError as error:
            logger.warning("Dispatch hold marker could not be given its ID: %s", type(error).__name__)

    def _restore_dispatch_hold(self, c):
        """At startup, restore holds whose marker outlived a failed or lost database write.

        Runs before anything can report a new hold, so it may also rewrite an ID-less
        (older or malformed) marker with its hold's ID; Resume then matches it by ID.
        """
        for backend in BACKENDS:
            self._restore_backend_hold(c, backend)

    def _restore_backend_hold(self, c, backend):
        marker = self._read_hold_marker(backend)
        if marker is None:
            return
        table = HOLD_TABLES[backend]
        row = c.execute(f"SELECT hold_id FROM {table} WHERE id=1").fetchone()
        if row is not None:
            if "hold_id" not in marker:
                hold_id = row["hold_id"] or uuid4().hex
                c.execute(f"UPDATE {table} SET hold_id=? WHERE id=1", (hold_id,))
                self._write_marker_id(marker, hold_id, backend)
            return
        if "hold_id" in marker and self._hold_released(c, marker["hold_id"]):
            # Resume committed this release; only removing its file was interrupted.
            try:
                (self.state_dir / HOLD_MARKERS[backend]).unlink(missing_ok=True)
            except OSError:
                pass
            return
        code, hold_id = marker.get("code", "unknown"), marker.get("hold_id") or uuid4().hex
        # An unreadable or partial marker still holds: releasing dispatch is the operator's decision.
        self._insert_dispatch_hold(c, {
            "hold_id": hold_id, "code": code,
            "message": ACCOUNT_HOLDS.get(code, UNREADABLE_HOLDS[backend]), "subject_id": marker.get("subject_id"),
            "worker_id": marker.get("worker_id"), "created_at": marker.get("created_at", self.clock()),
            "detail": marker.get("detail", "")}, backend, restored=True)
        if "hold_id" not in marker:
            self._write_marker_id(marker, hold_id, backend)

    def _release_dispatch_hold(self, c):
        """Inside Resume's transaction: release every copy of every backend's hold in the database only.

        Memory and the marker files change only after the commit (finish_dispatch_release),
        so a failed Resume leaves the holds exactly as they were; nothing here touches files.
        """
        released: list[str] = []
        for backend in BACKENDS:
            table = HOLD_TABLES[backend]
            row = c.execute(f"SELECT hold_id FROM {table} WHERE id=1").fetchone()
            pending, marker = self._pending_dispatch_holds.get(backend), self._read_hold_marker(backend)
            if row is None and pending is None and marker is None:
                continue
            ids = {hold_id for hold_id in ((row["hold_id"] if row else None), (pending or {}).get("hold_id"),
                                           (marker or {}).get("hold_id")) if hold_id}
            c.execute(f"DELETE FROM {table}")
            names = sorted(ids) or ["released-" + uuid4().hex]
            c.executemany("INSERT OR IGNORE INTO released_dispatch_holds(hold_id,released_at) VALUES(?,?)",
                          [(hold_id, self.clock()) for hold_id in names])
            released.extend(names)
        return released

    async def finish_dispatch_release(self):
        """After Resume commits, drop the in-memory and file copies of the holds it released."""
        for backend in BACKENDS:
            pending, marker = self._pending_dispatch_holds.get(backend), self._read_hold_marker(backend)
            ids = [hold_id for hold_id in ((pending or {}).get("hold_id"), (marker or {}).get("hold_id")) if hold_id]
            if not ids:
                continue
            released = await self.db.call(
                lambda c, ids=ids: {hold_id for hold_id in ids if self._hold_released(c, hold_id)})
            if (pending is not None and pending["hold_id"] in released
                    and self._pending_dispatch_holds.get(backend) is pending):
                del self._pending_dispatch_holds[backend]
            # Re-read just before removing, with nothing awaited in between: a newer hold may
            # have replaced the file while the database was consulted.
            current = self._read_hold_marker(backend)
            if current and current.get("hold_id") in released:
                try:
                    (self.state_dir / HOLD_MARKERS[backend]).unlink(missing_ok=True)
                except OSError as error:
                    # Startup recognizes the released ID and removes the file then.
                    logger.warning("Released dispatch hold marker could not be removed: %s", type(error).__name__)

    def _environment_hold(self) -> dict | None:
        """A hold on both backends while this machine cannot sandbox agents' commands. Unlike an
        account hold it ends by itself, without Resume, once the machine is fixed. In the pilot
        WSL turned interop back on under the running service, which had checked it only at its
        first Codex session: after a restart every Codex session failed its preflight in turn."""
        probe = self.environment_probe
        if probe is None:
            return None
        now, cached = time.monotonic(), self._environment
        if cached is None or now - cached[0] > ENVIRONMENT_SECONDS:
            blocker, previous = probe(), (cached[1] if cached else None)
            since = (cached[2] if cached and previous else self.clock()) if blocker else None
            if blocker != previous:
                if blocker:
                    logger.warning("New sessions are held: %s", blocker)
                else:
                    logger.warning("New sessions may start again: %s no longer applies", previous)
            cached = (now, blocker, since)
            self._environment = cached
        if cached[1] is None:
            return None
        return {"code": "sandboxUnavailable", "subject_id": None, "worker_id": None, "created_at": cached[2],
                "message": cached[1] + ". New sessions wait until it is fixed and then start without Resume."}

    def _dispatch_hold(self, c, backend=None):
        """The hold on one backend's workers; without a backend, the first hold in force."""
        for name in ([backend] if backend else BACKENDS):
            row = c.execute(f"SELECT code,message,subject_id,worker_id,created_at FROM {HOLD_TABLES[name]} "
                            "WHERE id=1").fetchone()
            if row:
                return {**dict(row), "backend": name}
            pending = self._pending_dispatch_holds.get(name)
            if pending is not None and not self._hold_released(c, pending["hold_id"]):
                return {**{key: pending[key] for key in ("code", "message", "subject_id", "worker_id", "created_at")},
                        "backend": name}
            # Every profile of the backend at its usage limit: ends by itself at the first reset.
            limited = getattr(self, "_account_profiles_hold", None)
            if limited is not None and (hold := limited(c, name)) is not None:
                return {**hold, "backend": name}
        environment = self._environment_hold()
        if environment is not None:
            return {**environment, "backend": backend or BACKENDS[0]}
        return None

    def _dispatch_holds(self, c):
        return {name: hold for name in BACKENDS if (hold := self._dispatch_hold(c, name)) is not None}

    def _require_dispatch(self, c, backend=None):
        hold = self._dispatch_hold(c, backend)
        if hold is not None:
            raise HubError("DISPATCH_HELD", hold["message"], 409)

    async def require_dispatch(self, backend=None):
        """The last check before new model input is sent to a backend's worker."""
        await self.db.call(lambda c: self._require_dispatch(c, backend))

    async def note_launch(self, kind, subject_id, receipt):
        """Record a launch before its process exists; no record means nothing was launched."""
        if kind not in {"worker", "helper"} or (receipt is not None and receipt.get("format") != 2):
            raise ValueError("Invalid runtime launch record")
        await self.db.call(lambda c: self._event(c, "execution.launch_started", subject_id,
                                               {"kind": kind, "receipt": receipt}), write=True)

    async def _recover_stopped_owners(self, run_id, *, exclude_attempts=(), exclude_helpers=()):
        """Probe exact OS ownership outside SQLite before closing orphaned work."""
        from .runtime.ownership import tree_quiet

        def owner(kind, row):
            return row["worker_id"] if kind == "worker" else row["source_worker_id"]

        def pending(c):
            scope = self._control_scope(c, run_id)
            if scope is not None and not scope:
                return []
            rows = [("worker", dict(r)) for r in c.execute(
                "SELECT a.* FROM attempts a JOIN workers w ON w.active_attempt=a.id "
                "WHERE a.run_id=? AND a.ended_at IS NULL "
                "AND NOT EXISTS(SELECT 1 FROM events e WHERE e.kind='execution.quiescence_verified' AND e.object_id=a.id)", (run_id,))]
            rows += [("helper", dict(r)) for r in c.execute(
                "SELECT * FROM helper_intents WHERE run_id=? AND state IN ('prepared','running','uncertain')", (run_id,))]
            result = []
            for kind, row in rows:
                if (row["id"] in (exclude_attempts if kind == "worker" else exclude_helpers)
                        or not self._scope_admits(c, run_id, scope, owner(kind, row))):
                    continue
                try:
                    saved = self._latest_event(c, row["id"], "execution.process_owned")
                    if saved:
                        record = json.loads(saved["payload_json"])
                        if record.get("kind") == kind:
                            result.append((kind, row, record["receipt"]))
                    elif kind == "worker" and row["state"] == "recovery_required":
                        # Claimed by an earlier coordinator that stopped before recording process
                        # ownership. Its launch record, written before any process exists, decides:
                        # none means nothing was launched; a planned cgroup is probed like a receipt.
                        launch = self._latest_event(c, row["id"], "execution.launch_started")
                        planned = json.loads(launch["payload_json"]).get("receipt") if launch else None
                        if launch is None or planned is not None:
                            result.append((kind, row, planned))
                except (ValueError, TypeError, KeyError, AttributeError) as error:
                    # One unreadable record leaves only that execution held; the rest recover.
                    logger.warning("Ownership record for %s is unreadable; it stays held: %s",
                                   row["id"], type(error).__name__)
            return result

        for kind, row, receipt in await self.db.call(pending):
            try:
                # A tree whose coordinator has died ends here, as that coordinator's unit
                # would have ended it.
                if receipt is not None and not await asyncio.to_thread(tree_quiet, receipt, end_orphan=True):
                    continue

                prepared = {} if kind == "helper" else await self.prepare_workspace_snapshots([row["worker_id"]])

                def close(c):
                    if not self._scope_admits(c, run_id, self._control_scope(c, run_id), owner(kind, row)):
                        return
                    if kind == "helper":
                        current_helper = self._helper(c, row["id"])
                        if current_helper["state"] not in {"prepared", "running", "uncertain"}:
                            return
                        c.execute("UPDATE helper_intents SET state='uncertain' WHERE id=?", (row["id"],))
                        if current_helper["state"] == "running" and not current_helper["raw_result_json"]:
                            self._hold_helper_usage(c, current_helper, "Owned helper process ended before a retained result")
                        if not self._latest_event(c, row["id"], "helper.execution_closed"):
                            self._event(c, "helper.execution_closed", row["id"], {"process_quiet": True, "source": "owned_tree_probe"})
                        return
                    worker = self._get(c, "workers", row["worker_id"])
                    current = self._get(c, "attempts", row["id"])
                    if worker["active_attempt"] != row["id"] or current["ended_at"] is not None:
                        return
                    self._dispatch(c, "runtime.abandon", {
                        "worker_id": row["worker_id"], "quiescent": True,
                        "reason": "The exact recorded native process job is now quiet; continue from retained history and files."},
                        Actor(role=Role.RUNNER))
                    self._event(c, "execution.quiescence_verified", row["id"], {"process_quiet": True, "receipt": receipt})

                await self.db.call(lambda c: self._with_deferred_files(
                    c, lambda c: self.with_workspace_snapshots(c, prepared, close)), write=True)
                await self.finish_deferred_files("execution recovery", archive_moves=False)
            except Exception:
                logger.exception("Owned execution recovery will retry for %s", row["id"])

    def _retry_quiet_helpers(self, c, run_id, scope=None):
        for row in c.execute("SELECT * FROM helper_intents WHERE run_id=? AND state='uncertain'", (run_id,)).fetchall():
            if not self._scope_admits(c, run_id, scope, row["source_worker_id"]):
                continue
            c.execute("SAVEPOINT quiet_helper_recovery")
            try:
                proof = self._latest_event(c, row["id"], "helper.execution_closed")
                if not proof or proof["created_at"] > self.clock() - 30000 or not json.loads(proof["payload_json"]).get("process_quiet"):
                    continue
                if row["raw_result_json"]:
                    # Retry the retained decision's commit, never inference.
                    c.execute("UPDATE helper_intents SET state='running' WHERE id=?", (row["id"],))
                else:
                    # A new decision decides from current facts.
                    c.execute("UPDATE helper_intents SET state='cancelled',ended_at=?,reason='automatic_retry_after_error' WHERE id=?",
                              (self.clock(), row["id"]))
                    if row["kind"] == "recruitment":
                        c.execute("UPDATE task_lifecycle SET decision_helper_id=NULL WHERE decision_helper_id=? "
                                  "AND recruitment_decision='pending'", (row["id"],))
                    # A cancelled assignment decision is decided again at the next admission.
                self._event(c, "helper.retry_ready", row["id"], {"retained_decision": bool(row["raw_result_json"])})
            except Exception:
                c.execute("ROLLBACK TO quiet_helper_recovery")
                logger.exception("Helper recovery will retry for %s", row["id"])
            finally:
                c.execute("RELEASE quiet_helper_recovery")

    def _retry_admission_failures(self, c, run_id, scope=None):
        rows = c.execute("SELECT e.*,w.current_task_id,w.work_intention FROM events e JOIN workers w ON w.id=e.worker_id "
                         "WHERE e.kind='worker.admission_failed' AND w.run_id=? AND w.state='recovery_required' "
                         "AND w.active_attempt IS NULL AND e.created_at<=? AND e.sequence=(SELECT MAX(other.sequence) "
                         "FROM events other WHERE other.kind=e.kind AND other.worker_id=e.worker_id) "
                         "AND NOT EXISTS(SELECT 1 FROM attempts a WHERE a.worker_id=w.id AND a.created_at>=e.created_at AND a.state<>'preparation_failed') "
                         "AND NOT EXISTS(SELECT 1 FROM events retried WHERE retried.kind='worker.admission_retry' "
                         "AND retried.object_id=w.id AND retried.sequence>e.sequence)",
                         (run_id, self.clock() - 30000)).fetchall()
        for row in rows:
            if not self._scope_admits(c, run_id, scope, row["worker_id"]):
                continue
            c.execute("SAVEPOINT admission_recovery")
            try:
                proof = json.loads(row["payload_json"])
                if proof.get("attempt_admitted") is not False:
                    continue
                c.execute("UPDATE workers SET state=CASE WHEN EXISTS(SELECT 1 FROM interruption_checkpoints i "
                          "WHERE i.worker_id=workers.id AND i.state='parked') THEN 'suspended' ELSE 'idle' END "
                          "WHERE id=?", (row["worker_id"],))
                if row["current_task_id"] == row["task_id"] and row["work_intention"] == "working":
                    c.execute("UPDATE task_members SET runnable=1,work_reason='admission_retry' "
                              "WHERE worker_id=? AND task_id=? AND work_reason='workspace_recovery_required'",
                              (row["worker_id"], row["task_id"]))
                    self._recompute_task(c, row["task_id"])
                self._event(c, "worker.admission_retry", row["worker_id"], {"reason": "Retry preparation; retain all working files"})
            except Exception:
                c.execute("ROLLBACK TO admission_recovery")
                logger.exception("Admission recovery will retry for %s", row["worker_id"])
            finally:
                c.execute("RELEASE admission_recovery")

    async def prior_execution_warning(self, worker_id, attempt_id):
        def read(c):
            # A hold before any input (a pause, an account or a catalog hold) sent nothing: the
            # warning is about the last activation that did.
            prior = c.execute("SELECT state FROM attempts WHERE worker_id=? AND id<>? "
                              "AND state NOT IN ('preflight_held','catalog_held') "
                              "ORDER BY created_at DESC,rowid DESC LIMIT 1", (worker_id, attempt_id)).fetchone()
            lost = ("Terminal sessions and background commands from that turn no longer exist; "
                    "rerun any command whose result you still need.")
            if prior and prior["state"] in {"abandoned", "message_interrupted"}:
                return ("The previous activation encountered an infrastructure error and its processes have stopped. "
                        "Inspect retained conversation and current files, then choose your next action. "
                        "An uncertain operation may already have taken effect; do not blindly repeat it. " + lost)
            if prior and prior["state"] == "interrupted":
                return "Your previous turn was stopped before it finished. " + lost
            return None
        return await self.db.call(read)

    def _refresh_usage_uncertainty(self, c, run_id):
        uncertain = any(c.execute(query, (run_id,)).fetchone() for query in (
            "SELECT 1 FROM worker_usage_holds WHERE run_id=?",
            "SELECT 1 FROM turn_intents WHERE run_id=? AND state='uncertain'",
            "SELECT 1 FROM helper_usage_holds WHERE run_id=?",
        ))
        c.execute("UPDATE runs SET usage_uncertain=? WHERE id=?", (int(uncertain), run_id))

    def _runtime_retry_delay(self, c, attempt):
        """Derive restart-stable backoff from consecutive identical ended failures."""
        signature = None
        count = 0
        for prior in c.execute("SELECT id,state FROM attempts WHERE worker_id=? "
                               "ORDER BY created_at DESC,rowid DESC LIMIT 16", (attempt["worker_id"],)):
            if prior["state"] not in {"abandoned", "preflight_held", "catalog_held", "message_interrupted"}:
                break
            event = self._latest_event(c, prior["id"], "runtime.preflight_held", "runtime.catalog_held",
                                       "attempt.abandoned", "worker.message_interrupted")
            if event is None:
                break
            payload = json.loads(event["payload_json"])
            reason = str(payload.get("reason") or payload.get("error") or prior["state"])
            reason = re.sub(r"[0-9a-f]{8}-[0-9a-f-]{27,}|[0-9a-f]{32,}", "<id>", reason[:500])
            current = hashlib.sha256((event["kind"] + ":" + reason).encode()).hexdigest()
            if signature is not None and signature != current:
                break
            signature = current
            count += 1
        delay = min(60 * 60 * 1000, 60_000 * 2 ** max(0, count - 1))
        if count >= 5 and not c.execute(
                "SELECT 1 FROM events WHERE kind='runtime.repeated_failure' AND object_id=? "
                "AND json_extract(payload_json,'$.signature')=?", (attempt["worker_id"], signature)).fetchone():
            self._event(c, "runtime.repeated_failure", attempt["worker_id"], {
                "signature": signature, "consecutive_failures": count,
                "retry_after_ms": delay, "operator_notice": True,
                "reason": "Repeated infrastructure failure; automatic recovery backs off without pausing worker intent."})
        return delay

    async def recover_quiet_runtime_failures(self, run_id, *, exclude_attempts=(), exclude_helpers=()):
        """Retry stopped, evidenced executions; never replay unknown paid work.

        A persisted end timestamp supplies a small retry delay across restarts.
        Worker decisions to wait/sleep and operator control remain authoritative.
        Unknown results stay unknown; recovery creates a new continuation only
        after the old invocation's process ownership has ended.
        """
        await self._recover_stopped_owners(run_id, exclude_attempts=exclude_attempts, exclude_helpers=exclude_helpers)
        await self.finish_deferred_files("recovery", archive_moves=False)  # Task files whose retry is due.

        def recover(c):
            # While the run is paused, only individually resumed workers recover.
            scope = self._control_scope(c, run_id)
            if scope is not None and not scope:
                return []
            self._retry_quiet_helpers(c, run_id, scope)
            self._retry_admission_failures(c, run_id, scope)
            recovered = []
            rows = c.execute(
                "SELECT a.* FROM workers w JOIN attempts a ON a.worker_id=w.id "
                "WHERE w.run_id=? AND w.state='recovery_required' AND w.active_attempt IS NULL "
                "AND (w.work_intention='working' OR a.state IN ('message_interrupted','preflight_held','catalog_held')) AND a.ended_at<=? "
                "AND a.state IN ('abandoned','preflight_held','catalog_held','message_interrupted') "
                "AND a.id=(SELECT id FROM attempts WHERE worker_id=w.id ORDER BY created_at DESC,rowid DESC LIMIT 1)",
                (run_id, self.clock() - 30000)).fetchall()
            for row in rows:
                worker_id = row["worker_id"]
                if not self._scope_admits(c, run_id, scope, worker_id):
                    continue
                delay = self._runtime_retry_delay(c, row)
                if row["ended_at"] + delay > self.clock():
                    continue
                c.execute("SAVEPOINT quiet_worker_recovery")
                try:
                    intent = c.execute("SELECT * FROM turn_intents WHERE attempt_id=? ORDER BY created_at DESC,rowid DESC LIMIT 1",
                                       (row["id"],)).fetchone()
                    # Whether the attempt's own records prove that nothing of it still runs.
                    closed = self._latest_payload(c, row["id"], "attempt.abandoned", "worker.message_interrupted")
                    if row["state"] in {"preflight_held", "catalog_held"} and intent is None:
                        proof = self._latest_payload(c, row["id"], "runtime.preflight_held", "runtime.catalog_held")
                        safe = proof.get("process_quiet") is True and proof.get("inference_dispatched") is False
                    elif intent and intent["state"] == "closed_unknown":
                        safe = self._latest_payload(c, intent["id"], "turn.execution_closed").get("process_quiet") is True
                    elif intent and intent["state"] in {"completed", "interrupted", "failed", "cancelled", "not_sent"}:
                        proof = self._latest_payload(c, intent["id"], "turn.finished", "turn.not_sent")
                        safe = proof.get("process_quiet") is True and (
                            (intent["state"] == "not_sent" and proof.get("inference_dispatched") is False)
                            or (proof.get("terminal_observed") is True and proof.get("status") == intent["state"]))
                    else:
                        safe = intent is None and closed.get("process_quiet") is True
                    if not safe and closed.get("process_quiet") is not True:
                        continue
                    if (row["state"] in {"preflight_held", "catalog_held"}
                            and self._get(c, "workers", worker_id)["work_intention"] == "waiting"):
                        if not safe:
                            continue
                        # Preserve voluntary waiting after preflight failure.
                        # Only a message-only attempt reopens message delivery;
                        # no mathematical task is forced to resume.
                        c.execute("UPDATE workers SET state='suspended',pending_wake=CASE WHEN ? THEN 1 ELSE pending_wake END WHERE id=?",
                                  (row["execution_mode"] == "message_only", worker_id))
                        self._event(c, "worker.automatically_recovered", worker_id,
                                    {"attempt_id": row["id"], "preserved_intention": "waiting"}, row["task_id"], worker_id)
                        recovered.append(worker_id)
                        continue
                    result = self._lifecycle_command(c, "worker.recover_message" if row["state"] == "message_interrupted" else "worker.recover", {
                        "worker_id": worker_id, "process_confirmed_stopped": True,
                        "reason": "Automatic recovery after a retained terminal/quiescence receipt. Continue the original conversation; do not replay the previous turn."},
                        Actor(role=Role.OPERATOR))
                    if result and (result.get("directive") == "recovered" or row["state"] == "message_interrupted"):
                        self._event(c, "worker.automatically_recovered", worker_id,
                                    {"attempt_id": row["id"]}, row["task_id"], worker_id)
                        recovered.append(worker_id)
                except Exception:
                    c.execute("ROLLBACK TO quiet_worker_recovery")
                    logger.exception("Automatic recovery deferred for worker %s", worker_id)
                finally:
                    c.execute("RELEASE quiet_worker_recovery")
            return recovered
        return await self.db.call(recover, write=True)

    def _close_stopped_history(self, c, worker, payload):
        """Apply an operator's stopped-process evidence to earlier runtime records."""
        if (worker["active_attempt"] is not None or worker["state"] != "recovery_required"
                or payload.get("process_confirmed_stopped") is not True or not payload.get("reason")):
            return
        for row in c.execute("SELECT DISTINCT a.id FROM attempts a JOIN turn_intents t ON t.attempt_id=a.id "
                             "WHERE a.worker_id=? AND a.ended_at IS NOT NULL "
                             "AND t.state IN ('prepared','sending','bound','uncertain')", (worker["id"],)).fetchall():
            self._close_failed_native_turns(c, worker["id"], row["id"], payload["reason"])

    def _close_failed_native_turns(self, c, worker_id, attempt_id, reason):
        """End execution ownership, never invent a model outcome or replay it.

        Only call after the runner has collected all owned processes. A later
        continuation is a new input in the original conversation, with the old
        outcome and accounting uncertainty still retained for inspection.
        """
        for intent in c.execute("SELECT * FROM turn_intents WHERE worker_id=? AND attempt_id=? "
                                "AND state NOT IN ('not_sent','reconciled')", (worker_id, attempt_id)).fetchall():
            proof = self._latest_payload(c, intent["id"], "turn.finished", "turn.terminal_reconciled",
                                         "turn.execution_closed")
            terminal_known = (intent["state"] in {"completed", "interrupted", "cancelled", "failed"}
                              and proof.get("terminal_observed") is True and proof.get("status") == intent["state"])
            if not terminal_known:
                c.execute("UPDATE turn_intents SET state='closed_unknown' WHERE id=?", (intent["id"],))
                if proof.get("status") != "unknown":
                    # A turn finished as unknown was held then (`turn.finish`); hold it once.
                    self._hold_worker_usage(c, worker_id, "Stopped execution has an unknown native outcome")
            self._event(c, "turn.execution_closed", intent["id"], {
                "attempt_id": attempt_id, "previous_state": intent["state"], "process_quiet": True,
                "status": intent["state"] if terminal_known else "unknown",
                "terminal_observed": terminal_known, "reason": reason, "previous_turn_replayed": False}, worker_id=worker_id)
