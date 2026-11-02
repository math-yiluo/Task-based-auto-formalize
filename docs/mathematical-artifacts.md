# Mathematical artifacts

The Lean application accepts three mathematical contribution kinds: `definition`, `statement`, and `proof`. They describe the contribution, not a fixed reasoning phase. Workers choose their investigations, helper tasks, and waits. Environments, build reports, and review reports support these contributions without becoming a fourth mathematical category.

## Submitted pair and record

A contribution is submitted as its entry-module Lean file with a nonempty UTF-8 Markdown file at the same path stem: `Directory/Name.lean` pairs with `Directory/Name.md`. The Markdown explains the Lean content for review. Every Lean file in a workspace snapshot and in the Library has its own nonempty same-stem Markdown.

Every mathematical record includes:

- `bundle.snapshot`: the immutable source snapshot of the worker workspace.
- `bundle.lean_path`, `bundle.lean_sha256`, `bundle.markdown_path`, `bundle.markdown_sha256`.
- `entry_module` and `declaration`, identifying its Lean declaration.
- `environment_artifact` and `environment_version`.

Records carry no references to other mathematical artifacts. The worker workspace is a checkout of the current Library at a recorded base commit; work imports published mathematics by Lean name. The `stage`/`contribute` boundary derives the snapshot, pair hashes, and environment version from the actual files and task. See the packaged [submission guide](../src/formal_geyser/submission-guide.md).

Source scans check every Lean file of a snapshot. They reject the proof holes and axioms `sorry`, `admit`, `axiom` and `sorryAx`, and the constructs that record declarations the kernel never checked or run code while the source compiles: `debug.skipKernelTC`, `addDeclWithoutChecking`, `extern`, `implemented_by`, `#eval`, `#eval!`, `run_cmd`, `run_elab`, `run_meta`, `initialize` and `builtin_initialize`, also inside dotted or `«quoted»` names. Comments and string literals are distinguished from executable tokens. Project source cannot shadow the toolchain's `Init`, `Lean` or `Std` modules.

The construct list is a second, deliberately incomplete defense. Compilation trusts what elaboration records, so every official build is followed by a kernel replay (`lean_tools/KernelReplay.lean`, the algorithm of Lean's `leanchecker`): every declaration of the built project modules goes to the kernel again, and a theorem the kernel never checked is rejected. The inspector also checks the submitted declaration's transitive assumptions against the environment's allowlist.

## Definition

A `definition` record describes a Lean definition or defined type. Its Markdown explains the objects, parameters, conventions, and assumptions precisely enough to compare with the Lean declaration.

For source-backed work, `provenance: source` requires precise source locations and exact source versions. The reviewer compares the source, Lean, and Markdown. Newly introduced helpers may instead use `provenance: derived` or `internal` with an explicit `provenance_rationale`.

`lean.definition@1` validates the record, builds the frozen source tree with Lake under an authorized environment, replays the built modules through the kernel, and checks the declaration kind. The independent review establishes the intended mathematical interpretation.

## Statement

A `statement` record describes a proposition definition: `def` or `abbrev` with result type `Prop`. Defining a proposition records what is being asserted; it does not assert that the proposition is true.

`lean.statement@1` performs the same checks as definition publication, then requires a proposition-returning definition. The reviewer checks the source/Lean/Markdown correspondence, including quantifiers, hypotheses, ambient structures, and conventions.

## Proof

A proof task describes its target in prose and pins its environment. A `proof` record names the Library statement it proves in `statement_declaration`, together with its environment, task ID/revision, and its own Lean/Markdown pair.

`lean.proof@1` builds the candidate workspace over its Library base with Lake, replays the built project modules through the kernel, checks that the proof is a theorem whose type is the named statement, and requires the statement's file, every project file it imports, and their Markdown to keep their base bytes, so a proof cannot weaken the proposition it claims. The statement must already be in the Library at the candidate's base. The Markdown explains the proof actually implemented in Lean; it need not follow the source author's route. See [Lake validation](lake-validation.md).

`lean.development@1` provides development evidence and does not accept a proof. `lean.build@1` records compilation and kernel replay only.

## Review and publication

Staging does not complete the host task. `stage` creates a frozen candidate and an independent review while the author may continue working; `contribute` records contribution intent and drains the activation. Completion is declared separately with accepted and published evidence.

The dedicated Integrator integrates accepted candidates in a fixed batch. Contributions keep no fixed place in the Library: the Integrator may split, merge, move, and rename files, modules, and declarations, keeping every reviewed meaning and every Lean file paired with matching Markdown. Final validation scans, builds and replays the complete final tree once; there are no per-item checks at integration. See [DESIGN.md](../DESIGN.md).

After publication, the submitted record and its receipts move to the accepted archive. Later Library changes never invalidate earlier acceptances. An error found later in the Library is corrected by an ordinary reviewed contribution that edits those files.

Snapshot count and byte limits come from the selected environment and apply to normalization and validation.

## Inspect the schemas

From the application checkout:

```bash
uv run formalizectl schema definition
uv run formalizectl schema statement
uv run formalizectl schema proof
```

These commands print the actual strict Pydantic schemas and start no workers.
