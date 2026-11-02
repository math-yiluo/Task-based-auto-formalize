# Test runs and intermittent failures

Run the suite with `uv run python scripts/test_parallel.py`. Each partition has
its own temporary directory, log and JUnit report; the final summary prints their
location. Keep the host awake so process timeouts describe running time.

The suite runs on the Linux host ([the Linux host](linux-host.md)): it starts real processes
in real cgroups, so each test process moves into a delegated scope of the user's systemd
manager the first time it starts one. The tests' projects keep their operator tokens in a
temporary directory (`FORMAL_GEYSER_RUNTIME_DIR`), not in the user's runtime directory.

For a diagnostic run, use:

```bash
uv run python scripts/test_parallel.py --rerun-failures
```

After all parallel partitions finish, the runner reruns each failed test once in
its own pytest process, serially. Parametrized cases retain their exact node IDs.
`isolated-*.log` and `isolated-*.xml` record these extra attempts. The initial
counts, failure reports and nonzero exit status remain unchanged even if every
isolated attempt passes. A skipped or missing isolated result is not a pass.
Reruns are opt-in and do not change service or production retry behavior.

## Git-backed failures to investigate

Intermittent failures around managed `empty-hooks` and `empty-attributes` remain
unconfirmed as a shared-state bug. Repository initialization and ownership checks
use instance-local control paths under the repository lock; test instances use
separate temporary roots. Do not remove these checks or retry production Git
writes based solely on an isolated test pass.

For the next occurrence, retain the original traceback, failing node ID, control
path, partition log and isolated result. Distinguish missing/modified safety files
from a Git process timeout, and record whether the machine slept. Earlier timeout
failures followed sleep; that does not establish the cause of a different file-access failure.
