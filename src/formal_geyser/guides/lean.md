# Lean authoring guide

Definitions and statements must agree with their Markdown and cited original
literature, and the Markdown claims no more than the Lean states or proves; explain
the provenance of internal or derived content. Proof Markdown
describes the implemented Lean proof, whose route may differ from the literature.
Published mathematics has no artifact identity: find what you need in the current
Library by its Lean names and import it. A proof proves a statement already in the
Library, which its task names.

Compile and check with `lean_check`: Lean runs only in the project's Lean service,
never in your shell, with the prepared environment in `paths`. Mathlib's sources
are readable there; never download or rebuild Mathlib.

Keep unfinished Lean in `.private/drafts/<name>/`: `lean_check` with that
`directory` compiles it together with your workspace's other Lean files, and
staging ignores it. Staging snapshots the rest of your workspace, so every Lean
file there must be paired and free of proof holes. Stage each reviewable unit as
soon as it compiles with complete Markdown; never pause or end a turn with
finished work unstaged.

Compilation and staging do not establish mathematical acceptance: publication
requires independent semantic review, and a proof also the Lake build and
exact-target checks. Read `.private/instructions/lean-operations.md` when checking
declarations or submitting; submission guides and schemas are in `paths`.
