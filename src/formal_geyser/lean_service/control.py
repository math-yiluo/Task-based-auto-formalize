"""Operator commands for a project's Lean service: start, stop, restart, status, telemetry."""
from __future__ import annotations

import asyncio
import time
from pathlib import Path

from .client import HttpClient, ServiceUnavailable, state_directory


async def _ended(client: HttpClient, instance: str | None, seconds: float = 120.0) -> bool:
    """Whether that instance has gone; observing never starts the service."""
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        try:
            if (await client.status(start=False)).get("instance") != instance:
                return True
        except ServiceUnavailable:
            return True
        await asyncio.sleep(0.5)
    return False


async def control(root: Path, command: str, *, hours: float = 24.0) -> dict:
    client = HttpClient(root)
    if command == "start":
        await client.ensure_running()
        return {"running": True, **await client.status()}
    if command == "status":
        try:
            return {"running": True, **await client.status(start=False)}
        except ServiceUnavailable:
            return {"running": False}
    if command == "telemetry":
        try:
            return await client.telemetry(hours)
        except ServiceUnavailable:
            return {"running": False}
    if command not in {"stop", "restart"}:
        raise ValueError("Unknown command: " + command)
    if command == "stop":
        (state_directory(root) / "stop").touch()  # The supervisor does not start it again.
    try:
        instance = (await client.status(start=False)).get("instance")
    except ServiceUnavailable:
        if command == "restart":
            await client.ensure_running()
            return {"running": True, **await client.status()}
        return {"running": False}
    # Running jobs are queued again and continue when the service next starts.
    try:
        await client.shutdown()
    except ServiceUnavailable:
        pass  # It ended meanwhile.
    if not await _ended(client, instance):
        raise RuntimeError("The Lean service did not stop within two minutes")
    if command == "stop":
        return {"running": False, "stopped_instance": instance}
    await client.ensure_running()
    return {"running": True, "previous_instance": instance, **await client.status()}
