# Independent review operation reference

Find named paths in `paths` or `paths.reference_index`. Begin with `review`: the
candidate record and paired files, the host task's acceptance conditions, the
machine validation (`review.validation`), the exact decision basis
(`review.decision_basis`; copy its fields verbatim into the verdict) and the
current phase. Read references marked `required: true`; full history is optional.
Native reading is preferred; `workspace_read` and `resource_read` are fallbacks for
authorized supplied paths.

Use `taskhub_actions(operation="review")` for the exact verdict schema and
`taskhub_call(operation="review", arguments=...)` to submit. Acceptance requires
concrete findings and evidence, and no unresolved qualifications; mathematical
artifacts also require the domain semantic evidence. Reject demonstrated defects
with specific revision suggestions. Unavailable tools or inaccessible literature
are unresolved verification, not rejection: `request` with `kind: "literature"`
names the source, and `pause` with `{"external_response_id": "<request id>"}` waits
for it.

An acceptance may carry `integration_notes` with what your duplication and
placement checks found: `overlaps` (each existing declaration, module or page, its
`relation`: duplicate, generalizes, specializes or related, and a `note` on whether
one could replace the other without changing any meaning), `placement` (where the
files belong and why) and `remarks`. They guide the Integrator's organization only
and are not acceptance evidence: an unresolved doubt is a qualification, which
blocks acceptance.

All reviewed contributions have source files to review and integrate, including
public wiki and supporting notes. There is one independent reviewer per candidate,
and authorship excludes self-review. Review is based on the candidate, Library,
wiki, original literature and task conditions. You may read project notes and ask
other workers; a reply arrives as a message turn, where `resume_work` continues the
review, and it never supplies independent approval. Public drafts and DAGs may help
orientation but do not establish accepted results. You may maintain the shared
planning DAGs; those edits need no extra review and grant no acceptance.

After acceptance, end this turn. The Integrator owns the separate integration
batch; you do not prepare, validate, or publish a merge. A question it has about
this artifact later starts a new conversation from your review report and permits a
scoped reply to the Integrator; it does not reopen ordinary work or authorize a
mathematically different candidate.

Recover the original review after process failure; failure is not a verdict.
Reassignment requires confirmed unrecoverability and ended old execution. An
accepted review keeps its exact decision and may wait for integration without any
active model process. Review turns and checks have no time limit.
