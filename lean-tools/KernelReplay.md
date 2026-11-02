# Kernel replay

This helper sends every declaration recorded by the requested compiled modules to the
kernel again, using Lean's own replay (the algorithm of the toolchain's `leanchecker`).
Their other imports are loaded once and trusted as compiled. Compilation alone trusts
what elaboration recorded, so `debug.skipKernelTC` or an unchecked addition could record
a theorem the kernel never checked; replay rejects it. A kernel rejection is reported as
a finding naming the module; a failure to load the environment is not a finding.
