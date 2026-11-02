# Batch operations

1. `batch_status` shows the fixed membership and current round, with each
   contribution's reviewer `integration_notes` when given.
2. `batch_prepare` returns an isolated combined workspace. Edit it with native
   file tools. Conflict markers identify the contributing artifact; resolve them.
   Use `batch_resolve(paths)` to stage resolved or intentionally added files;
   untracked files are not automatically included.
   After deferral, preparing again creates a new workspace without the deferred
   item's changes. Earlier workspaces are retained for inspection.
3. `batch_validate` freezes that complete tree and returns per-candidate machine
   reports, exact integration IDs, commit, and snapshot. If the actual diff is
   only unpaired public Markdown under `wiki/` or `notes/`, it checks readable nonempty pages
   and the README without launching Lean. You check entries and local links. Other changes retain domain checks.
   A failed or unavailable round grants no publication authority. You may correct
   meaning-preserving engineering issues or defer an item, then validate again.
4. Before publishing, make sure the validated tree keeps the whole Library
   semantically consistent (see the Integrator guide). The original independent
   reviews remain the per-item semantic acceptance; no confirmation file is needed.
5. `batch_publish(round_id)` submits this exact validated round and drains.
   The coordinator publishes one commit, records each original review separately,
   archives each artifact, and then notifies its task.

`batch_defer(candidate_id, reason)` retains the item outside this batch and keeps
its independent acceptance/history. Contact the author or reviewer using
`talk_send(recipient_id, text)` and name the artifact ID in the text.
`batch_retry` returns an item to a future batch only when new information resolves
its blocker. When no item remains included (defer each one, a withdrawn one too),
call `batch_finish_empty`; it publishes nothing.
In a correspondence-only turn, answer messages and use `batch_retry` if justified;
do not attempt integration without a current work assignment.
