from typing import Any


class HubError(Exception):
    def __init__(
        self, code: str, message: str, status: int = 409,
        *, details: dict[str, Any] | None = None,
    ):
        self.code = code
        self.message = message
        self.status = status
        self.details = details
        super().__init__(message)

    def as_dict(self) -> dict:
        result: dict[str, Any] = {"code": self.code, "message": self.message}
        if self.details is not None:
            result["details"] = self.details
        return result


class ValidationContinues(Exception):
    """A plugin's validation goes on outside this process after its caller stopped waiting.
    Asking for the same validation again attaches to it or returns its result."""

    def __init__(self, message: str, details: dict[str, Any] | None = None):
        super().__init__(message)
        self.details = details or {}


class WorkerWorkspaceError(HubError):
    """A local preflight failure, with identity retained across claim rollback."""

    def __init__(self, worker_id: str, task_id: str, message: str):
        self.worker_id = worker_id
        self.task_id = task_id
        super().__init__("WORKER_WORKSPACE_UNAVAILABLE", message)
