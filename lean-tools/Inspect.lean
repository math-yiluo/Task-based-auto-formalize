module

import Lean
import all Lean.CoreM

open Lean Meta

private def declarationKind : ConstantInfo → String
  | .axiomInfo _ => "axiom"
  | .defnInfo _ => "definition"
  | .thmInfo _ => "theorem"
  | .opaqueInfo _ => "opaque"
  | .quotInfo _ => "quotient"
  | .inductInfo _ => "inductive"
  | .ctorInfo _ => "constructor"
  | .recInfo _ => "recursor"

private def statementTarget (info : ConstantInfo) : MetaM (Option Expr) := do
  let .defnInfo definition := info | return none
  -- An inductive type application cannot reduce to Prop itself. Avoid
  -- opening expensive instance parameters for ordinary data definitions.
  if let .const name _ := definition.type.getForallBody.getAppFn then
    if let .inductInfo _ ← getConstInfo name then return none
  forallTelescope definition.type fun parameters result => do
    let .sort .zero ← whnf result | return none
    let value ← whnf (mkAppN definition.value parameters)
    return some (← mkForallFVars parameters value)

/-- The nodes left of `budget` once `e` is written out in full, with each shared subterm spelled
out at every use as `Expr.repr` writes it; none once the budget runs out, so the count costs at
most `budget` steps. -/
private partial def nodesLeft (e : Expr) (budget : Nat) : Option Nat :=
  if budget == 0 then none else
  match e with
  | .app f a => nodesLeft f (budget - 1) >>= nodesLeft a
  | .lam _ t b _ | .forallE _ t b _ => nodesLeft t (budget - 1) >>= nodesLeft b
  | .letE _ t v b _ => nodesLeft t (budget - 1) >>= nodesLeft v >>= nodesLeft b
  | .mdata _ b | .proj _ _ b => nodesLeft b (budget - 1)
  | _ => some (budget - 1)

/-- Each distinct subterm of `e` once, as a line `N kind fields…`, numbered in the order the
subterms are first reached, children before their parent and named by their numbers. -/
private partial def sharedLines (e : Expr) : StateM (Std.HashMap Expr Nat × Array String) Nat := do
  if let some number := (← get).1[e]? then return number
  let fields ← match e with
    | .bvar index => pure s!"bvar {index}"
    | .fvar id => pure s!"fvar {repr id.name}"
    | .mvar id => pure s!"mvar {repr id.name}"
    | .sort level => pure s!"sort {repr level}"
    | .const name levels => pure s!"const {repr name} {repr levels}"
    | .app f a => do pure s!"app {← sharedLines f} {← sharedLines a}"
    | .lam name t b info => do pure s!"lam {repr name} {repr info} {← sharedLines t} {← sharedLines b}"
    | .forallE name t b info => do pure s!"forallE {repr name} {repr info} {← sharedLines t} {← sharedLines b}"
    | .letE name t v b nonDep => do
      pure s!"letE {repr name} {nonDep} {← sharedLines t} {← sharedLines v} {← sharedLines b}"
    | .lit literal => pure s!"lit {repr literal}"
    | .mdata data b => do pure s!"mdata {repr data} {← sharedLines b}"
    | .proj name index b => do pure s!"proj {repr name} {index} {← sharedLines b}"
  modifyGet fun (seen, lines) =>
    (lines.size, (seen.insert e lines.size, lines.push s!"{lines.size} {fields}"))

/-- Written out in full with more nodes than this, an exact expression is written with its
sharing (`sharedLines`) rather than by `Expr.repr`: in the Linux pilot statements written out
ran to 7 GB, and checks that wrote one took more memory than the machine had. -/
private def reprNodes : Nat := 1000000

/-- An exact expression and how it is written: `repr`, as `Expr.repr` writes it, or `shared`,
each distinct subterm once (`sharedLines`), whose size follows the subterms that differ. -/
private def render (e : Expr) : String × String :=
  if (nodesLeft e reprNodes).isSome then ("repr", reprStr e)
  else ("shared", "\n".intercalate ((sharedLines e).run ({}, #[])).2.2.toList)

/-- An inspection's small fields, and its exact expressions (`render`): the declaration's type
and, for a statement definition, its proposition, which can be far larger than the rest. -/
private structure Inspection where
  fields : List (String × Json)
  typeRepr : String
  targetRepr : Option String

private def inspect (moduleName declaration : Name) (statement : Option Name) : MetaM Inspection := do
  let env ← getEnv
  let info ← getConstInfo declaration
  let target ← statementTarget info
  let mut targetMatches := false
  let mut statementModule := ""
  if let some statementName := statement then
    let statementInfo ← getConstInfo statementName
    statementModule := ((env.getModuleIdxFor? statementName).map fun idx =>
      env.header.moduleNames[idx.toNat]!.toString).getD moduleName.toString
    if let some expected ← statementTarget statementInfo then
      if info.levelParams.length == statementInfo.levelParams.length then
        let actual := info.type.instantiateLevelParams info.levelParams
          (statementInfo.levelParams.map Level.param)
        targetMatches ← isDefEq actual expected
  let axioms ← collectAxioms declaration
  let origins := axioms.toList.map fun ax =>
    let origin := (env.getModuleIdxFor? ax).map fun idx => env.header.moduleNames[idx.toNat]!
    (ax.toString, toJson (origin.map Name.toString |>.getD ""))
  let (typeEncoding, typeRepr) := render info.type
  let renderedTarget := target.map render
  return {
    fields := [
      ("axioms", toJson (axioms.map Name.toString)),
      ("axiom_modules", Json.mkObj origins),
      ("declaration", toJson declaration.toString),
      ("kind", toJson (declarationKind info)),
      ("level_parameters", toJson (info.levelParams.map Name.toString)),
      ("module", toJson moduleName.toString),
      ("statement_returns_prop", toJson target.isSome),
      ("statement_module", toJson statementModule),
      ("target_matches", toJson targetMatches),
      ("type_encoding", toJson typeEncoding),
      ("statement_target_encoding", toJson (renderedTarget.map (·.1)))]
    typeRepr
    targetRepr := renderedTarget.map (·.2) }

/-- Load the compiled module. A failure here concerns the environment, such as a
library file that could not be read under memory pressure, not the inspected input. -/
private def load (sysroot : String) (moduleName : Name) : IO (Except String Environment) := do
  try
    initSearchPath sysroot
    return .ok (← importModules #[{ module := moduleName, importAll := true }] {} (trustLevel := 0))
  catch error =>
    return .error error.toString

private def availableInspection (action : MetaM Inspection) : MetaM (Except String Inspection) := do
  try
    return .ok (← action)
  catch error =>
    if error.isMaxHeartbeat || error.isMaxRecDepth then
      return .error (← error.toMessageData.toString)
    throw error

public def main (args : List String) : IO UInt32 := do
  let (inputs, outputFile) := match args.reverse with
    | output :: "--output" :: rest => (rest.reverse, some output)
    | _ => (args, none)
  match inputs with
  | sysroot :: moduleName :: declaration :: rest =>
    match ← load sysroot moduleName.toName with
    | .error message =>
      IO.eprintln s!"Environment unavailable: {message}"
      return 3
    | .ok env =>
    try
      let action := inspect moduleName.toName declaration.toName (rest.head?.map String.toName)
      let observed ← Lean.Core.CoreM.toIO' (availableInspection action).run'
        { fileName := "TaskBasedInspector", fileMap := FileMap.ofString "",
          options := maxHeartbeats.set {} 0, maxHeartbeats := 0 }
        { env := env }
      let .ok inspection := observed
        | IO.eprintln "Inspection unavailable: resource exhaustion"; return 3
      match outputFile with
      | some path =>
        -- Each exact expression goes to its own file beside the result, which stays small.
        IO.FS.writeFile (path ++ ".type_repr") inspection.typeRepr
        if let some target := inspection.targetRepr then
          IO.FS.writeFile (path ++ ".statement_target_repr") target
        IO.FS.writeFile path (Json.mkObj
          (("protocol", toJson "task-based-lean-inspect@3") :: inspection.fields)).compress
      | none =>
        IO.println (Json.mkObj (("protocol", toJson "task-based-lean-inspect@1")
          :: ("type_repr", toJson inspection.typeRepr)
          :: ("statement_target_repr", toJson inspection.targetRepr) :: inspection.fields)).compress
      return 0
    catch error =>
      -- Report the finding itself: Lean also exits with 1 on an internal panic,
      -- such as running out of memory, which is not a finding.
      IO.println <| (Json.mkObj [
        ("protocol", toJson "task-based-lean-inspect@1"),
        ("finding", toJson error.toString)]).compress
      IO.eprintln error.toString
      return 1
  | _ =>
    IO.eprintln "Usage: Inspect.lean SYSROOT MODULE DECLARATION [STATEMENT] [--output FILE]"
    IO.eprintln "With --output, FILE.type_repr and FILE.statement_target_repr hold the exact expressions."
    return 2
