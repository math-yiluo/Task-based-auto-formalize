"""Offline revalidation of a staged definition, statement, or proof.

--record appends actual fresh machine evidence; it never rewrites a receipt,
accepts a semantic review, resumes a worker, or publishes to the Library.
"""
from __future__ import annotations

import argparse
import asyncio
import json
from pathlib import Path

from formal_geyser.maintenance import run


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--project", type=Path, required=True)
    parser.add_argument("--artifact", required=True)
    parser.add_argument("--record", action="store_true", help="Append the verifier's actual result as new evidence.")
    args = parser.parse_args()
    result = asyncio.run(run(args.project.resolve(), args.artifact, record=args.record))
    print(json.dumps(result, indent=2, ensure_ascii=False))
    return 0 if result["report"]["verdict"] == "accepted" and not result["report"]["qualifications"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
