# Lean review guide

Machine validation accepted these exact bytes before your review began: the Lake
build of the whole workspace over its Library base, the kernel replay of every
module, the source scan (no `sorry`, `admit`, `#eval`, `run_cmd`, `initialize`,
`extern`, `implemented_by` or `debug.skipKernelTC`), the allowed-assumption check
(no `native_decide` or new axioms), statements being `Prop`-valued definitions, and
for a proof that the theorem's type is its named statement, whose files and imports
are unchanged. Do not rebuild to verify any of this again.

What no machine checks is meaning, and that is your review. Actually read the cited
ORIGINAL literature at its edition and page, section, definition, or theorem;
summaries and compiler success cannot establish fidelity.

- For definitions and statements, check correspondence among the original
  literature, the Markdown and the Lean: objects, hypotheses, quantifiers,
  conventions, scope and conclusions. For internal or derived content, verify its
  provenance rationale.
- Check the elaborated meaning, not only the text: instances, implicit arguments,
  coercions and universes, and whether a statement is vacuous or trivially
  provable. A small `lean_check` probe settles such a question: copy the
  candidate's Lean files to their module paths in `.private/drafts/<name>/`, add a
  module with an `example` or `#check` against its declarations, and check that
  module (`.private/instructions/lean-operations.md`).
- For a proof, compare the implemented Lean proof with its Markdown explanation and
  check that `statement_declaration` names the statement the task asks for. A
  valid proof may take a different route from the literature.

Record concrete findings with original-source locators and exact paired `path:line`
ranges. Reject demonstrated mathematical defects; inaccessible literature leaves
the review unresolved. Never repair the mathematics and approve your replacement.
Read [Lean review operations](lean-review-operations.md), at
`.private/instructions/lean-review-operations.md`, when recording a verdict.
