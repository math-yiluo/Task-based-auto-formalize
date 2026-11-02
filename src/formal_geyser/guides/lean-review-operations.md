# Lean review operation reference

Read this when recording a verdict on a Lean candidate. Named reference paths are in
`paths`, or in the file at `paths.reference_index`: read `semantic_review_guide`,
`semantic_review_schema` and `semantic_review_example` before submitting.

## Semantic evidence

An acceptance carries `semantic_checks`:

- `source_checks`: for definitions and statements, one three-way finding (original
  literature, Markdown, Lean) for every cited `(literature_id, content_sha256,
  locator)`, copied exactly.
- `lean_markdown`: the Lean against its Markdown; for a proof, the implemented proof
  against its explanation.
- `provenance_rationale`: for source-free derived or internal content.

Every finding names the exact paired paths with lines, `path:line` or
`path:start-end`, such as `Project/Statement.lean:4-8` and
`Project/Statement.md:3-6`, and says what agrees or fails. A declaration name alone,
"read the files" or "compiles" is no finding. Every `lean_location` and
`markdown_location` is in the candidate's primary pair (`review.candidate.lean` and
`review.candidate.markdown`), never another file or an imported module, which you name
in the finding text instead; each location is one line or one start-end range, and
each further range needs another finding.

## Verdict

Assess author `data.qualifications` against the pinned task and explain the
disposition in the rationale; scope notes do not by themselves prevent acceptance.
Make sure the paired Markdown states the essential scope and assumptions for future
Library users. Your report's `qualifications` records unresolved verification,
which blocks acceptance.

Reject mathematical errors and mistranslations with the exact defect, evidence and
revision suggestions. An inaccessible original source is unresolved verification,
not a rejection. Your acceptance is the semantic acceptance of the exact candidate
against its fixed Library version; it does not publish.
