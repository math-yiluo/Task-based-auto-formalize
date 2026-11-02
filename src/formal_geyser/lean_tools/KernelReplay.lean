/-
Kernel replay of compiled project modules (protocol task-based-lean-replay@1).

Every declaration the requested modules recorded is sent to the kernel again by
`Lean.Kernel.Environment.replay`, against one environment of their other imports,
which are trusted as compiled. This is the algorithm of the toolchain's `leanchecker`
(LeanChecker.lean, Copyright (c) 2023 Kim Morrison, Sebastian Ullrich, Apache 2.0),
loading the imported environment once for all requested modules rather than once
for each of them.
-/
import Lean.CoreM
import Lean.Replay

open Lean

/-- All data parts of a compiled module: exported, server and private. -/
unsafe def moduleParts (module : Name) : IO (Array (ModuleData × CompactedRegion)) := do
  let mFile ← findOLean module
  unless (← mFile.pathExists) do
    throw <| IO.userError s!"object file '{mFile}' of module {module} does not exist"
  let mut fnames := #[mFile]
  let sFile := OLeanLevel.server.adjustFileName mFile
  if (← sFile.pathExists) then
    fnames := fnames.push sFile
    let pFile := OLeanLevel.private.adjustFileName mFile
    if (← pFile.pathExists) then
      fnames := fnames.push pFile
  readModuleDataParts fnames

/-- The requested modules' constants, where each came from, and their other imports. -/
unsafe def load (modules : Array Name) :
    IO (Environment × Std.HashMap Name ConstantInfo × Std.HashMap Name Name) := do
  let requested : NameSet := modules.foldl (·.insert ·) {}
  let mut imports : Array Import := #[]
  let mut constants : Std.HashMap Name ConstantInfo := {}
  let mut origin : Std.HashMap Name Name := {}
  for module in modules do
    let parts ← moduleParts module
    let some (data, _) := parts[0]? | throw <| IO.userError s!"failed to read module data of {module}"
    for i in data.imports do
      unless requested.contains i.module do
        imports := imports.push i
    -- The last ("most private") part subsumes the earlier ones.
    let some (last, _) := parts.back? | throw <| IO.userError s!"failed to read module data of {module}"
    for name in last.constNames, info in last.constants do
      constants := constants.insert name info
      origin := origin.insert name module
  let (_, state) ← importModulesCore imports |>.run
  let env ← finalizeImport state imports {} 0 false false (isModule := true)
  return (env, constants, origin)

/-- The declaration that `replay` names when the kernel rejects one. -/
def replayedDeclaration (message : String) : Option Name :=
  match message.splitOn "while replaying declaration '" with
  | _ :: rest :: _ => (rest.splitOn "'").head?.map String.toName
  | _ => none

unsafe def main (args : List String) : IO UInt32 := do
  let [sysroot, requestFile, outputFile] := args
    | IO.eprintln "Usage: KernelReplay.lean SYSROOT REQUEST OUTPUT"; return 2
  -- A failure before the replay concerns the environment, such as a compiled file
  -- that could not be read, not the replayed declarations.
  let loaded ← try
      initSearchPath sysroot
      let request ← IO.ofExcept (Json.parse (← IO.FS.readFile requestFile))
      let names ← IO.ofExcept (request.getObjValAs? (Array String) "modules")
      let modules := names.map String.toName
      pure (Except.ok (modules, ← load modules))
    catch error =>
      pure (Except.error error.toString)
  match loaded with
  | .error message =>
    IO.eprintln s!"Environment unavailable: {message}"
    return 3
  | .ok (modules, env, constants, origin) =>
    try
      discard <| env.toKernelEnv.replay constants
      IO.FS.writeFile outputFile (Json.mkObj [
        ("protocol", toJson "task-based-lean-replay@1"), ("verdict", toJson "verified"),
        ("modules", toJson modules.size), ("constants", toJson constants.size)]).compress
      return 0
    catch error =>
      let message := error.toString
      let module := (replayedDeclaration message).bind (origin.get? ·)
      let finding := Json.mkObj [
        ("protocol", toJson "task-based-lean-replay@1"), ("finding", toJson message),
        ("module", toJson (module.map toString))]
      IO.FS.writeFile outputFile finding.compress
      IO.println finding.compress
      IO.eprintln message
      return 1
