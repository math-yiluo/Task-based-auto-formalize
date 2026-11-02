"""Optional local service fallback: restart a missing service, which then resumes its driven runs."""
from __future__ import annotations

import argparse
import json
import os
import socket
import subprocess
import sys
import time
import urllib.request
from pathlib import Path

import portalocker

from .local_operator import bearer


class ServiceSupervisor:
    def __init__(self, project: Path, run_id: str, port: int = 8765):
        self.project = project.resolve()
        self.run_id = run_id
        self.port = port
        self.state_dir = self.project / ".task-based"
        self.process: subprocess.Popen | None = None
        self.retry_at = 0.0

    def request(self, method, path):
        request = urllib.request.Request(f"http://127.0.0.1:{self.port}{path}", method=method,
            data=b"" if method == "POST" else None, headers=bearer(self.project, "taskhub"))
        with urllib.request.urlopen(request, timeout=5) as response:
            return json.load(response)

    def port_open(self):
        try:
            with socket.create_connection(("127.0.0.1", self.port), timeout=1):
                return True
        except OSError:
            return False

    def start_service(self):
        # A reachable or still-starting service is never killed for slow I/O.
        # The service's existing ProjectOwner lock remains the final authority.
        with (self.state_dir / "supervised-service.log").open("ab") as output:
            self.process = subprocess.Popen(
                [sys.executable, "-m", "formal_geyser.coordination", "--project", str(self.project),
                 "serve", "--host", "127.0.0.1", "--port", str(self.port)],
                stdin=subprocess.DEVNULL, stdout=output, stderr=subprocess.STDOUT)
        return self.process.pid

    def tick(self):
        try:
            run = self.request("GET", f"/v1/runs/{self.run_id}")
        except Exception as error:
            if self.port_open() or (self.process is not None and self.process.poll() is None):
                return {"state": "waiting_for_service", "error": type(error).__name__}
            if time.monotonic() < self.retry_at:
                return {"state": "retry_pending"}
            self.retry_at = time.monotonic() + 15
            return {"state": "service_started", "pid": self.start_service()}
        # The service re-arms runs that were driven before it stopped. Supervision
        # never drives, resumes, or recovers on its own, so --no-resume holds.
        return {"state": "observing", "run_state": run["state"], "armed": bool(run.get("armed"))}

    def run(self):
        self.state_dir.mkdir(parents=True, exist_ok=True)
        stop = self.state_dir / "supervisor.stop"
        with portalocker.Lock(str(self.state_dir / "supervisor.lock"), mode="a+b", timeout=0):
            (self.state_dir / "supervisor.json").write_text(json.dumps({"pid": os.getpid(),
                "run_id": self.run_id, "project": str(self.project), "started_at": time.time()}, indent=2), encoding="utf-8")
            previous = None
            while not stop.exists():
                try:
                    status = self.tick()
                except Exception as error:
                    status = {"state": "retry_pending", "error": type(error).__name__}
                if status != previous:
                    try:
                        with (self.state_dir / "supervisor-events.jsonl").open("a", encoding="utf-8") as log:
                            log.write(json.dumps({"time": time.time(), **status}) + "\n")
                    except OSError:
                        # A transient diagnostics failure must not end supervision.
                        # Retain the old status so the next tick retries this record.
                        pass
                    else:
                        previous = status
                time.sleep(5)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--project", type=Path, required=True)
    parser.add_argument("--run-id", required=True)
    parser.add_argument("--port", type=int, default=8765)
    args = parser.parse_args()
    if not 1 <= args.port <= 65535:
        parser.error("port must be between 1 and 65535")
    ServiceSupervisor(args.project, args.run_id, args.port).run()


if __name__ == "__main__":
    main()
