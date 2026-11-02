"""Child-process library check for tooling.validate_library_isolated.

Reads one JSON request on stdin and writes one JSON result on stdout. Its hashing
threads hold this process's GIL, never the coordination service's.
"""
from __future__ import annotations

import json
import os
import sys
from pathlib import Path

from formal_geyser.coordination.storage import BlobStore
from formal_geyser.registry import RegistryError, check_library


def main() -> int:
    request = json.loads(sys.stdin.buffer.read())
    try:
        root, seen, rehashed = check_library(
            BlobStore(Path(request["blobs"])), request["location"], request["manifest_sha256"],
            reuse_digest=request.get("reuse_digest"))
    except RegistryError as error:
        result = {"ok": False, "error": str(error)}
    else:
        result = {"ok": True, "root": str(root), "digest": seen, "rehashed": rehashed, "pid": os.getpid()}
    sys.stdout.write(json.dumps(result))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
