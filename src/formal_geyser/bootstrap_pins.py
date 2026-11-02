"""The one supported, immutable Lean/Mathlib baseline for new project instances."""

LEAN_VERSION = "4.34.0"
LEAN_TOOLCHAIN = "leanprover/lean4:v4.34.0"
LEAN_COMMIT = "293d5d0c0c3f3dded4688b3ccd6a33939ac5102b"
MATHLIB_REPOSITORY = "https://github.com/leanprover-community/mathlib4.git"
MATHLIB_COMMIT = "5ed2965256430c3649e86755f9576b54eca72435"
MATHLIB_TAG = "v4.34.0"
CACHE_KEY = f"lean-{LEAN_COMMIT[:12]}-mathlib-{MATHLIB_COMMIT[:12]}"


def pinned_versions() -> dict[str, str]:
    return {"lean_toolchain": LEAN_TOOLCHAIN, "lean_commit": LEAN_COMMIT,
            "mathlib_repository": MATHLIB_REPOSITORY, "mathlib_commit": MATHLIB_COMMIT}
