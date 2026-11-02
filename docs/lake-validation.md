# Lake validation

Machine validation uses the pinned Lean and Lake executables, an incremental
build, a kernel replay, and inspection of the submitted declaration's kind, exact
proposition target and transitive assumptions. After every official build,
`lean_tools/KernelReplay.lean` (the algorithm of Lean's `leanchecker`) sends every
declaration of the built project modules to the kernel again, loading their other
imports once and trusting them as compiled; development checks skip it. A kernel
rejection is a finding naming the module; a replay that cannot load its environment
is unavailable. A closure whose exact compiled bytes already passed is not replayed
again. Definitions, proposition statements and proofs retain independent semantic
review against their Markdown and cited literature. Module-wide axiom audits are
not run.

## Persistent builds

Each instance owns `.task-based/objects/lake/workspaces/<environment>/<slot>/` and
`.task-based/objects/lake/cache/<environment>/`. The key binds Lean, Lake, registered
prebuilt manifests and an explicit build recipe version. Comments and unrelated
implementation edits do not invalidate caches. Lake traces own dependency changes.
Project source cannot replace a registered prebuilt module (case-insensitive path
comparison); new modules under an existing namespace remain allowed.

Lake restores requested cached artifacts into the local slot. Only the coordinator
supplies the package configuration; no dependency checkout or third-party build
runs. Frozen inputs remain separate from outputs. Inspection reads outputs in
place while the slot and namespace locks remain held. Never reuse a reported
`build_directory` after its compilation context has closed.

Ordinary compilation errors retain completed cache outputs. Cache receipts record
bytes, not proof acceptance. Interrupted commands, source mutations or corrupt
outputs invalidate the affected workspace. Removing source deletes that module's
facets; Lake handles downstream rebuilds without clearing unrelated outputs.
A slot reuses its earlier outputs while each keeps the size, modification and change
times and file ID in its receipt, for development and formal checks alike. An output
whose identity differs is hashed and must match the receipt; otherwise the slot's
outputs are discarded and the build starts clean. Integration builds the complete
final tree once.

## Retention

Maintenance runs lazily at most once per minute, after acquiring the caller's
shared namespace lock. Sizing runs in a joined thread alongside the build; no
separate service is started and cleanup cannot outlive the namespace lock. It retains at most two
idle slots per namespace, expires idle data after 24 hours, and targets 4 GiB for
disposable cache/workspace data across the instance. Oldest idle namespaces are
evicted first. These are retention limits, not execution limits: active namespaces
are never evicted and may exceed the target. The most recently used namespace
also survives size pressure alone: 4 GiB is a soft target, not a maximum Library
size. Its idle slot-count and age policies still apply on idle maintenance passes.
Stable lock files are retained. Size walks skip both active workspace trees and
their artifact caches, including older builds holding only slot locks. Missing
files during enumeration are ignored. The collector rechecks locks before eviction.
Builds waiting for maintenance's namespace lock yield asynchronously and can be
cancelled; a cache size walk cannot block the coordinator's event loop.
`retention.json` and build reports record observed idle size (`size_scope`),
reclaimed bytes, skipped active namespaces and cleanup errors. This is a partial
sample, not the total size of all live caches. Source Library, artifact archives, receipts
and `.dependencies` are outside this collector. Maintenance errors do not stop work.

## Targets and outcomes

Statements are proposition definitions. Proofs must prove their exact approved
statement, including parameters. The existing inspector returns the target's
axiom dependencies using Lean's compiled metadata. `sorryAx`, assumptions outside
the environment allowlist and axioms defined in the project output tree reject.
This checks the submitted declaration, not every unrelated declaration in a file.
Source pairing and the explicit-hole prohibition remain mandatory. A warning's
absence after cache restoration is never evidence that a target has no holes.

Deterministic heartbeat/recursion errors during candidate compilation are source
findings: the author must revise the code or its explicit options. They do not
prove the mathematical statement false. Inspector exhaustion, process interruption,
out-of-memory and unreadable imports remain unavailable. Neither outcome pauses
the entire project.

Unavailable original checks retry locally after 5 minutes, doubling up to a
3-hour backoff. Only generation zero can run in author preflight; later retries
use the background driver and cannot hold up the author's next turn.
Each attempt appends evidence under a new idempotency key; no
model inference or submitted bytes are replayed or changed. Rejected and
operator-needed results do not auto-retry. Pauses and changed input bases remain
effective. The integration queue displays the missing validation and retry time.
An operator can queue a retry through `validation.retry` with `artifact_id` and
a nonempty `reason`, without stopping the service.

## Where builds run

Every build, kernel replay and inspection runs in the project's Lean service, which
schedules them across the machine's cores and memory and runs each Lean process in the
Codex sandbox; see [lean-service.md](lean-service.md). The status API and dashboard show its
running, queued and suspended jobs. Lean elaboration can perform IO: the sandbox lets it
write only its build slot, the Lake cache and its job's scratch directory. This accepted
execution boundary does not weaken target checks or independent semantic review.

## Environment pins and existing instances

New captures record `lake_executable_sha256` as well as the compiler pin and
library manifests. Older immutable checker registrations may supply Lake's hash
from their already registered full-toolchain manifest. Nothing implicitly adopts
a changed executable. Environments with neither form of pin require an explicit
new environment capture/approval; they report unavailable in the meantime.
Historical records and receipts are not rewritten. No checker process is used.

Restart while paused to load these changes; resuming is a separate operator action.
`formalizectl revalidate` can create new evidence for an original candidate. It
does not supply semantic acceptance or publish the Library.
