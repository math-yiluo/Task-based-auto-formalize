# Coordination storage

SQLite remains the transactional ledger, not the authority for authored content.
Tasks, notes, wiki, source pairs and conversations retain their native file paths.
This change neither changes mathematical acceptance nor starts a run.

## Read and write boundaries

- One writer connection serializes mutations, delivery receipt creation, native
  file reconciliation and recovery. WAL and `synchronous=FULL` remain enabled.
- Four thread-affine connections open the existing database with `mode=ro` and
  `query_only=ON`. Only explicitly audited getters use them: SQL-only ones (run/worker
  status and listings, restart admission metadata, pending-message signals, the list
  of files an Integrator batch view will check), and the file-backed views a worker
  reads most, a task (`inspect`) and an artifact or record (`fetch`), which open and
  check their files there instead of on the writer (`NativeWorkflow._native_view`).
  Each callback holds one read transaction and releases it on success or failure. A
  record the archive mover moved between that snapshot and its file read is read again
  on a newer snapshot.
- `Database.call(..., write=False)` does not imply read-only. For example, reading
  unread messages can create delivery receipts and must stay on the writer.
  Each thread's per-call verification cache of file-backed views is cleared before
  each writer call and each read, so it never outlives one operation. A check kept
  across calls (a task revision,
  a pinned archive, a blob) is keyed by the ledger identities it was checked against
  and reused only while the checked files keep their size, modification time and ID.
  An Integrator batch view makes those checks on a worker thread before its call.
- The writer page cache is 64 MiB; each reader's cache is 8 MiB. Temporary SQL
  storage is not forced into memory. Startup schema/recovery runs once on the
  writer, before any reader opens; readers never run recovery or clear native caches.

## Exact receipts and machine reports

`artifact.accept` returns the candidate ID, current status, decision and new
evidence identity, rather than the complete artifact and its previous evidence.
`integrator.prepare.reserve` stores only the source identities/hashes needed to
compose the batch. Full artifact history remains available through `fetch` and
the artifact API. Retrying a committed command returns its original result.

A native call's core response refers to its canonical command receipt instead of
copying that body again. The backend resolves and checks the reference before
recovery uses it. The exact final model-visible reply is still retained separately.
Command keys do not expire: a long pause must not turn a retry into a new mutation.

Machine report details larger than 4 KiB are canonicalized, hashed and compressed
once under `.task-based/reports/<prefix>/<sha256>.json.gz`. Installation is atomic
and finishes before SQLite commits a reference. Identical bodies share bytes but
never share verdict identities or skip validation. A failed transaction may leave
an unreferenced report; it cannot leave a committed reference to a partial file.

The ledger stores a small summary, exact snapshot/candidate identities, a
`report_file` reference and the aggregate process-quiescence result. Any nested
negative quiescence result remains negative. Original compiler stdout/stderr and
all other report fields remain available in the compressed JSON; they are not
truncated there. Small reports stay inline. Batch projections can repeat small
summaries, but large whole-tree output is stored only once.

Report paths are relative to the current instance's `.task-based/`, so backup
relocation does not change normalized command hashes. The local owner API
`GET /v1/reports/<sha256>` returns the full verified JSON.
Missing or corrupt bodies produce an explicit error. Backup and restore must
include `.task-based/reports/` as well as the database and other durable content.
Do not collect receipts, reports or blob snapshots merely because they are old.

## Workspace preparation

Workspace traversal, file hashing and immutable blob writes run outside SQLite's
executor. Candidate capture retains its bounds, link checks and two exact passes,
which read every file; so does the check before an integration checkout is removed.
File metadata is not substituted for that content evidence. Other snapshots
(checkpoints, a turn's end) also compare two captures, but a file keeps the hash
the previous snapshot of its directory read while its size, modification and
change times, inode and device are unchanged and it had not changed for 2 s
before that snapshot; a file written within the same timestamp tick could
otherwise keep its times. The kernel sets the change time on every write and no
unprivileged process can set it back. A Library refresh tells a worker's own
changes from Library files by the same hashes. The short commit rechecks
worker/attempt/session/epoch, task revision and recorded Library base before
using the prepared snapshot. Snapshots and Library refreshes for one worker share
a filesystem lock.

Claiming work reserves its existing attempt and fence, then prepares the Library
checkout outside the writer. `attempt_workspaces` records that boundary;
`turn.reserve` refuses inference before it. The run driver does not wait for the
checkout while it admits: each activation prepares its own, at most four at once,
before its first model turn (`next_assignment(prepare=False)`). Other callers get the
assignment only after the checkout and base record commit.

Admission reads only what it may admit. One query lists the reviews, then the other
tasks, that may have saved work, already in admission order (pinned, then priority and age); each
is checked exactly and read in full only when its turn comes. Resumption tickets of
busy workers and message wakes of workers with work in hand are not tried. Open tasks
nobody works on yet are not read at all while none can get a worker: no idle worker
on an account with a free session and no session for a new worker, or no session for a
new worker while an assignment decision runs. While a decision runs, an open task that
would wait for another decision is not read in full either. An exact claim replay cannot refresh
an already prepared workspace. Interrupted preparation remains recoverable under
the existing no-model-dispatched rules. Local preparation failures are isolated
to that worker; unrelated admission remains available.

This removes the large workspace operations from transactions. It does not
pretend that every filesystem operation is gone: small native-file installs and
publication/reconciliation still retain their existing transactional boundaries.
Git expected-head checks, publication recovery and process quiescence are unchanged.

## Usage

Each `(worker_id, epoch)` retains one current cumulative sample in both native
and normalized accounting. Equal totals do not charge twice; regressions still hold
uncertain accounting and never refund tokens. Runs retain their charged totals. `turn_usage_totals` keeps integer
start/end totals per turn; exact conversations and native transcripts remain files.
Helper accounting still deduplicates incremental samples by sample ID, because
those are deltas rather than ordinary cumulative worker totals.

Assignment decisions keep their small offer (the idle workers and their previous task
titles) inline.

## Maintenance and rollout

New databases enable `auto_vacuum=INCREMENTAL`. The service attempts a bounded
256-page incremental-vacuum/passive-checkpoint pass every 30 minutes, only when
the writer queue is idle. Busy readers do not force a blocking checkpoint.
There is no online full `VACUUM`, age-based command deletion or automatic blob GC.

Sanitized `runtime-events.jsonl` rotates at 16 MiB and retains four compressed
chunks. Rotation shares a lock across runners. This diagnostic retention does not
delete ledger events, requests, model inputs/responses or validation evidence.

Every project uses the aggregated accounting schema. Physical compaction is separate
offline work, with a backup and explicit verification of accounting and recovery
references. Do not assume an estimated final database size until a representative run is measured.
