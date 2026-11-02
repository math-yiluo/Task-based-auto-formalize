"""Reviewer sidebar and native-history selection checks use only synthetic data."""
from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest


def test_reviewer_sidebar_without_browser_or_models():
    executable = shutil.which("node")
    if executable is None:
        pytest.skip("Node is an optional development check, never a runtime dependency")
    script = Path(__file__).with_name("dashboard_reviewers.test.cjs")
    result = subprocess.run([executable, "--test", str(script)], capture_output=True,
                            text=True, encoding="utf-8", timeout=30, check=False)
    assert result.returncode == 0, result.stdout + result.stderr
