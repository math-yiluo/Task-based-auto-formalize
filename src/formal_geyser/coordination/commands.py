"""Command handlers registered by name, so a dispatcher is a table lookup."""
from __future__ import annotations

from collections.abc import Callable
from typing import Any, TypeVar

Handler = TypeVar("Handler", bound=Callable[..., Any])


class CommandTable:
    """Handlers by command name or, for a family of commands, by its prefix and a dot."""

    def __init__(self) -> None:
        self._handlers: dict[str, Callable[..., Any]] = {}

    def handles(self, *names: str) -> Callable[[Handler], Handler]:
        """Register a method as the handler of these commands."""
        def register(method: Handler) -> Handler:
            for name in names:
                self._handlers[name] = method
            return method
        return register

    def get(self, name: str) -> Callable[..., Any] | None:
        return self._handlers.get(name) or self._handlers.get(name.partition(".")[0] + ".")

    def names(self) -> frozenset[str]:
        return frozenset(self._handlers)
