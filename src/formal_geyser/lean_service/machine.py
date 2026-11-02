"""What this machine has free now: logical cores, physical and committed memory, CPU load."""
from __future__ import annotations

import os
from pathlib import Path


def logical_cores() -> int:
    return os.cpu_count() or 1


def memory() -> tuple[int, int] | None:
    """Available physical and committed bytes, not installed RAM."""
    try:
        fields = {key: int(value.strip().split()[0]) * 1024 for key, value in
                  (line.split(":", 1) for line in Path("/proc/meminfo").read_text().splitlines())}
        return fields["MemAvailable"], fields["MemAvailable"] + fields.get("SwapFree", 0)
    except (OSError, ValueError, KeyError):
        return None


class CpuMeter:
    """The share of all cores that was busy since the previous reading."""

    def __init__(self) -> None:
        self._previous = self._times()

    @staticmethod
    def _times() -> tuple[float, float] | None:
        """Idle and total time of all cores, from /proc/stat."""
        try:
            parts = [float(part) for part in Path("/proc/stat").read_text().splitlines()[0].split()[1:]]
            return parts[3] + parts[4], sum(parts)
        except (OSError, ValueError, IndexError):
            return None

    def busy(self) -> float | None:
        current = self._times()
        previous, self._previous = self._previous, current
        if current is None or previous is None or current[1] <= previous[1]:
            return None
        return max(0.0, min(1.0, 1 - (current[0] - previous[0]) / (current[1] - previous[1])))
