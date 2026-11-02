"""Shared capture envelope; exact Lean environment limits apply at validation."""

# A candidate snapshot holds its author's whole Library checkout. In the Linux pilot the Library
# reached 19,999 files on 2026-11-02 under the former cap of 20,000, which stopped integration and
# left no room for a candidate's own files.
MAX_SNAPSHOT_FILES = 100_000
FORMER_MAX_SNAPSHOT_FILES = 20_000
MAX_SNAPSHOT_BYTES = 1024 * 1024 * 1024


def snapshot_bounds(max_files: int, max_bytes: int) -> tuple[int, int]:
    """Validate physical bounds, including backend-composed archive/export trees.

    The constants cover every supported Lean environment. Composite storage may
    explicitly allow more than one source tree; exact environment schemas and
    validation still enforce their own source limits.
    """
    if type(max_files) is not int or max_files < 1:
        raise ValueError("Snapshot file limit must be a positive integer")
    if type(max_bytes) is not int or max_bytes < 1:
        raise ValueError("Snapshot byte limit must be a positive integer")
    return max_files, max_bytes
