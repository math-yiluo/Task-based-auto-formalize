# Semantic review report reference

Named reference paths are supplied under `paths`. If a path is absent inline,
find its name in the file referenced by `paths.reference_index`. Read only the
format or procedure needed for the current operation.

Read this reference when preparing operation `review` for a mathematical artifact.
Use `taskhub_actions(operation="review")` for its current action schema and
`taskhub_call(operation="review", arguments=...)` to submit the report.
`paths.semantic_review_schema` identifies the complete JSON Schema
for its arguments, and `paths.semantic_review_example` identifies a
minimal source-backed statement example. The schema's `semantic_checks` is
derived from the existing `SemanticReviewEvidence` model. It is format guidance;
backend validation still checks exact candidate bindings and acceptance evidence.

The example uses synthetic IDs, hashes, source locations, and findings. Replace
every one with the actual reviewed candidate and what you verified. It assumes
one cited source; more sources require corresponding entries.
Never submit a template as evidence of work you did not do.

## Exact report and findings

Pass the JSON object as `arguments` to that gateway call; `review` has no report-path
argument. Copy `candidate_id`, `candidate_hash`, `task_revision`, `input_refs`,
and `policy` from the exact current review binding. `verdict` is `accept` or
`reject` in the tool call; TaskHub records acceptance internally as `approve`.
Supply a concrete `rationale` and `evidence` references. Acceptance requires
complete `semantic_checks` and no unresolved qualifications. Rejection requires
concrete `suggestions`. Missing sources, unavailable environments, or transient
tool failure leave verification unresolved; checkpoint and wait instead of
inventing an acceptance or mathematical rejection.

An acceptance may also carry `integration_notes` for the Integrator: `overlaps`
with existing Library declarations or modules (full Lean names or module paths),
`placement` and `remarks`. Record there what your duplication and placement
checks found. The notes are not acceptance evidence.

Read author `data.qualifications` as scope notes, not as the review's unresolved
findings. Address them in the rationale against the pinned acceptance conditions
and verify that essential assumptions and limitations appear in the paired
Markdown. Accept only when the scope is appropriate and no verification remains
unresolved; otherwise reject a demonstrated defect or wait for missing evidence.
Machine validation already did the Lake build and, for a proof, the exact-target
check.

The `semantic_checks` object has three fields:

- `lean_markdown`: at least one finding with `lean_location`,
  `markdown_location`, and `finding`. Compare actual mathematical meaning and,
  for a proof, the implemented Lean proof with its explanation.
- `source_checks`: for definitions and statements, one or more three-way
  findings covering every exact citation in the candidate's `sources`. Each has
  the same paired locations and finding plus the citation's `literature_id`,
  `content_sha256`, and `locator`.
- `provenance_rationale`: for source-free derived/internal definitions and
  statements, explain the checked rationale for the meaning. An empty `source_checks` array does not excuse this field.

Locations must be the **exact paired source paths** in the candidate bundle,
followed by `:line` or `:start-end`, with positive line numbers: for example
`Project/Statement.lean:4-8` and `Project/Statement.md:3-6`. A module or declaration
name alone is insufficient, and a matching suffix on a different path is wrong.
Use actual line ranges read from those frozen files. Every `lean_location` and
`markdown_location` is in the candidate's primary pair (`review.candidate.lean`
and `review.candidate.markdown`, cited by their paths in the bundle), never another
file or an imported module, which you name in the finding text instead; each
location is one line or one start-end range, and each further range needs another
finding.

Each source-check identity must exactly match one submitted citation
`(literature_id, content_sha256, locator)`. Copy these three fields; a source
check accepts no other identity fields. Never replace the fingerprint with a
title or paraphrase the locator. Read the original
edition and passage, and explain the agreement or defect in its objects,
hypotheses, quantifiers, conventions, scope, and conclusion. Cover the complete
set of submitted tuples, including multiple locators for the same source.

A proof review need not follow the literature's proof route. Compilation success
is never semantic acceptance.

## After the verdict

Acceptance hands the exact candidate against its fixed Library version to the
Integrator; it does not publish. A later question about your review starts a new
conversation from your recorded report. Notes never receive mathematical acceptance
from a generic note review.
