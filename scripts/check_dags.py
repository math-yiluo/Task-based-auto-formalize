"""Check proposed DAG files from a source checkout without installing packages.

Usage: python scripts/check_dags.py path/to/definitions.json path/to/proofs.json
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from formal_geyser.coordination.dag_check import main  # noqa: E402

if __name__ == "__main__":
    raise SystemExit(main())
