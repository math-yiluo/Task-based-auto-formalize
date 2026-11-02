# Shared definition and proof DAGs

Maintain the two project-wide planning DAGs when discovering missing definitions,
needed lemmas, or a changed construction/proof route. Every active worker and
reviewer may update them, including ordinary workers in message turns. They have
no task owner and require no peer-review task. The Integrator only reads them.

Call `dag_status` before editing. Its `graphs` reference gives the exact
`definitions` and `proofs` paths and `revision` of one consistent pair. Search
both graphs to reuse existing nodes. The shared-DAG entry in `paths` is a prompt-time
reference; `graphs/current.json` is a convenience pointer that may lag after an
I/O failure. Follow the status response even if the pointer is still pending.
Never edit shared revision files or current.json directly. If `available` is
false, preserve the damaged revision. Reconstruct a replacement pair privately
from intact evidence and submit it normally, or request storage repair. Do not
replace lost planning with empty graphs just to clear an error.

## Format

Both files contain `{"nodes": [], "edges": []}` initially. Each node requires a
project-unique `id` and `title`; optional `description` and `references` (a list of
file/source links) hold brief context. Definition nodes belong in definitions.json;
theorem/lemma nodes belong in proofs.json, including statements written as Lean
proposition definitions. Unimplemented nodes need no Lean file or artifact.

An edge is `{"from": "D", "to": "E", "reason": "What E must provide for D."}`.
It means D depends on E. Put it in the file containing its `from` node. Targets
may be in either file. IDs are case-sensitive; links in `references` are context,
not edges. Reuse stable IDs, never merge objects solely because titles match.
Keep mathematical detail and alternative routes in existing Markdown and link it.
Record the current proposed dependencies; do not combine alternatives into a
misleading mandatory dependency list. Describe a deliberately mutual collection
as one planning node, rather than suppressing a real cycle to pass a check.

## Edit, check, save

1. Copy BOTH current files into `.private/dags/edit/` in your workspace and retain
   the revision you read. Change the relevant nodes and outgoing edges there.
   If native writes are unavailable, use `workspace_copy` from resource root
   `graphs`, with each source path relative to that root. Use `workspace_write`
   for edits: `offset` and `delete_count` select Unicode characters to replace,
   with `expected_sha256` from the last read/copy/write. This supports small edits
   in files larger than 1 MiB without sending the whole file. Omit `offset` only
   for a full write of at most 1 MiB. In message-only mode these fallbacks permit
   only destinations under `.private/dags/`.
2. Run the standalone script at `paths.dag_checker`, or the `dag_checker` entry
   in `paths.reference_index` when it is not inline:

   ```text
   python .private/taskhub-reference/check_dags.py .private/dags/edit/definitions.json .private/dags/edit/proofs.json
   ```

   Collect its terminal result. Fix every error and require exit code zero.
   The checker validates both files together, including cross-graph cycles;
   a missing implementation is allowed, but a missing referenced node is not.
3. Save through the existing gateway:

   ```json
   {
     "operation": "dag_update",
     "arguments": {
       "source_dir": ".private/dags/edit",
       "expected_revision": 0,
       "summary": "Describe the nodes or dependencies changed and why."
     }
   }
   ```

   Replace zero with the revision you actually read. The server validates the
   same captured bytes using the same checker. If the revision is stale, it
   merges non-overlapping node/edge changes and validates the combined pair before
   atomically checking and saving the current head. Overlap or invalid graphs are
   never silently resolved. On `DAG_REVISION_CONFLICT`, use `details.current_graph_ref` or `dag_status`
   to read the new pair, merge your edits into it, rerun the checker, and submit
   a new call with the new revision. Never merely
   increase expected_revision while resubmitting files based on an older pair.
   A successful save returns its committed revision even if navigation repair is
   pending. Do not resubmit that change; `dag_status` can retry pointer repair.

All edges here are expected planning dependencies. No automatic Lean dependency
extraction is implemented. A valid DAG does not prove mathematical correctness,
availability, acceptance, or task completion; use exact Library publications and
existing verification evidence for those questions. DAG edits do not change task
contracts, wait predicates, or publication state. Do not review the whole graph
every turn or add unsupported prerequisites just to make it look complete.
