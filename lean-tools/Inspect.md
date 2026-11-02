# Declaration and target inspection

This helper reads built declarations and reports their kind, exact type, and the
proposition value of a statement definition. For proofs it compares the theorem
type with the bound proposition. It also returns the submitted declaration's
transitive axioms and defining modules. The coordinator checks the pinned allowlist
and rejects project-local assumptions. It does not audit every declaration or
replay proofs. With `--output`, the exact expressions go to private files beside the
small result, and the coordinator hashes them as it reads them, whatever their size.
An expression is written as `Expr.repr` writes it unless, written out with each shared
subterm spelled out at every use, it has more than a million nodes; then each distinct
subterm is written once, as a numbered line naming its children by number. The result
says which (`type_encoding`, `statement_target_encoding`).
