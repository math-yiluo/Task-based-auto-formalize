from __future__ import annotations

from uuid import uuid4

from formal_geyser.coordination.models import (
    Actor,
    Command,
    TaskContract,
)

from .plugin import LeanPlugin


class LeanApplication:
    """Lean facade over the coordination runtime bundled in this application."""

    def __init__(self, hub):
        self.hub = hub

    async def publish(self, contract: TaskContract, actor: Actor, *, key: str | None = None) -> dict:
        LeanPlugin().validate_contract(contract)
        return await self.hub.execute(Command(
            name="task.publish", key=key or str(uuid4()),
            payload={"contract": contract.model_dump(mode="json")},
        ), actor)
